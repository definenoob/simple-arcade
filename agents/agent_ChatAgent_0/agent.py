import asyncio
import uuid
import json
from typing import Union, Literal, Dict, Any, List

import pygame
from pydantic import BaseModel, Field, ValidationError
from summoner.client import SummonerClient
from summoner.protocol.process import Direction
from Crypto.PublicKey import RSA

# Import cryptographic utilities
from crypto_utils import (
    load_private_key,
    load_public_key,
    sign_message,
    verify_signature,
)

# ---- Cryptographic Setup ---------------------------------------------------
# Load the client's own private key for signing outgoing messages.
CLIENT_PRIVATE_KEY = load_private_key("keys/client_private_key.pem")
CLIENT_PUBLIC_KEY_STR = load_public_key("keys/client_public_key.pem").export_key().decode('utf-8')


# ---- Type-Safe JSON-RPC 2.0 Data Models ------------------------------------
# Base models for the core data
class KeystrokeParams(BaseModel):
    key: str = Field(..., description="The name of the key that was pressed.")
    key_code: int = Field(..., description="The integer key code from Pygame.")
    event_type: Literal["key_down", "key_up"]
    modifiers: Dict[str, bool]

class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str = "keyboard.event"
    params: KeystrokeParams
    id: Union[str, int] = Field(default_factory=lambda: str(uuid.uuid4()))

class ServerNotificationParams(BaseModel):
    message: str

class ServerNotification(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: str
    params: ServerNotificationParams

# Models to understand incoming batch reports from other agents
class BatchReportParams(BaseModel):
    frameNumber: int
    deltaEvents: List[Dict[str, Any]]
    deltaTiming: int

class BatchReportRequest(BaseModel):
    jsonrpc: Literal["2.0"]
    method: Literal["batch.report"]
    params: BatchReportParams
    id: Union[str, int]

# Wrapper models that include the cryptographic signature
class SignedWrapper(BaseModel):
    """A wrapper to hold the original payload, signature, and public key."""
    payload: Dict[str, Any]
    signature: str
    public_key: str


# ---- Pygame and Client Initialization ---------------------------------------
pygame.init()
screen = pygame.display.set_mode((640, 480))
pygame.display.set_caption("Secure Pygame Keystroke Client (Press ESC to quit)")
event_queue = asyncio.Queue()
client = SummonerClient(name="PygameAgent_0")

# ---- Cryptographic Hooks ---------------------------------------------------

@client.hook(direction=Direction.SEND, priority=1)
async def sign_outgoing_message(payload: dict) -> dict:
    """
    Signs the payload dictionary before it's sent. The resulting message
    is a new dictionary containing the original payload and its signature.
    """
    try:
        payload_bytes = json.dumps(payload, sort_keys=True).encode('utf-8')
        signature = sign_message(payload_bytes, CLIENT_PRIVATE_KEY)

        signed_wrapper = SignedWrapper(
            payload=payload,
            signature=signature,
            public_key=CLIENT_PUBLIC_KEY_STR
        )
        return signed_wrapper.model_dump()
    except Exception as e:
        print(f"[SIGNING ERROR] Failed to sign outgoing message: {e}")
        return None # Returning None stops the message from being sent.

@client.hook(direction=Direction.RECEIVE, priority=1)
async def verify_incoming_message(payload: dict) -> Union[dict, None]:
    """
    Verifies incoming messages. It intelligently unwraps messages relayed by
    the server and silently ignores any message that is not a valid signature wrapper.
    """
    message_to_validate = None

    # Check for the summoner server's wrapper format.
    if isinstance(payload, dict) and "content" in payload and "remote_addr" in payload:
        # The actual signed message is inside the 'content' key.
        message_to_validate = payload['content']
    # If not wrapped, assume the payload is the signed message.
    elif isinstance(payload, dict):
        message_to_validate = payload

    # If we couldn't find a dictionary to validate, discard the message.
    if not isinstance(message_to_validate, dict):
        return None

    try:
        # Now, validate the extracted message.
        signed_wrapper = SignedWrapper.model_validate(message_to_validate)

        sender_public_key_str = signed_wrapper.public_key
        sender_public_key = RSA.import_key(sender_public_key_str.encode('utf-8'))
        original_payload_bytes = json.dumps(signed_wrapper.payload, sort_keys=True).encode('utf-8')

        if not verify_signature(original_payload_bytes, signed_wrapper.signature, sender_public_key):
            print(f"\r[AUTH ERROR] Invalid signature for the provided public key. Discarding.")
            return None

        return {
            "identity": sender_public_key_str,
            "payload": signed_wrapper.payload
        }
    except ValidationError:
        # This is an expected failure for non-signed server messages. Silently ignore.
        return None
    except Exception as e:
        print(f"\r[VERIFICATION ERROR] Discarding malformed or unverifiable message: {e}")
        return None


# ---- Type-Safe RPC Handlers ------------------------------------------------
@client.receive(route="")
async def receiver_handler(msg: dict) -> None:
    """
    Handles incoming notifications that have already been verified by the hook.
    This can process both simple notifications and batch reports.
    """
    if not isinstance(msg, dict) or "identity" not in msg or "payload" not in msg:
        return

    try:
        verified_identity_key = msg["identity"]
        payload = msg["payload"]
        identity_snippet = verified_identity_key.splitlines()[1][:20] + "..."

        # Try to parse as a simple server notification first.
        try:
            notification = ServerNotification.model_validate(payload)
            print(f"\r[FROM {identity_snippet} | {notification.method}]: {notification.params.message}")

        except ValidationError:
            # If it's not a notification, try to parse it as a batch report.
            try:
                report = BatchReportRequest.model_validate(payload)
                num_events = len(report.params.deltaEvents)
                frame_number = report.params.frameNumber
                print(f"\r[FROM {identity_snippet} | Batch Report]: Received frame {frame_number} with {num_events} events.")
            except ValidationError:
                # Ignore any other message types that don't match.
                pass
    except Exception:
        # General catch-all in case the outer `msg` structure is wrong.
        pass


@client.send(route="")
async def send_handler() -> dict:
    """
    Constructs the core JSON-RPC request for a single keystroke.
    The signing hook will wrap it before sending.
    """
    try:
        event: pygame.event.Event = await event_queue.get()
    except asyncio.CancelledError:
        raise

    mods = pygame.key.get_mods()
    modifiers = {"shift": bool(mods & pygame.KMOD_SHIFT), "ctrl": bool(mods & pygame.KMOD_CTRL), "alt": bool(mods & pygame.KMOD_ALT)}
    params = KeystrokeParams(key=pygame.key.name(event.key), key_code=event.key, event_type="key_down" if event.type == pygame.KEYDOWN else "key_up", modifiers=modifiers)
    
    request_model = JsonRpcRequest(params=params)
    return request_model.model_dump()


# ---- Main Application Loop -------------------------------------------------
async def pygame_loop():
    """The main loop that runs Pygame and handles shutdown."""
    print("Window is active. Press keys to send events. Press ESC to quit.")
    running = True
    while running:
        try:
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                    running = False
                elif event.type in (pygame.KEYDOWN, pygame.KEYUP):
                    await event_queue.put(event)
            await asyncio.sleep(0.01)
        except asyncio.CancelledError:
            running = False

    print("Shutdown initiated. Cancelling tasks...")
    current_task = asyncio.current_task()
    for task in asyncio.all_tasks():
        if task is not current_task:
            task.cancel()


if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(pygame_loop())
        client.run(host="127.0.0.1", port=8888)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Shutdown requested.")
    finally:
        if pygame.get_init():
            pygame.quit()
        print("Client and Pygame shut down cleanly.")