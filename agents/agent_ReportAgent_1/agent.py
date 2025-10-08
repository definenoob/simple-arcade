import asyncio
import uuid
import json
import time
from typing import Union, Literal, Dict, Any, List

from pydantic import BaseModel, Field, ValidationError
from summoner.client import SummonerClient
from summoner.protocol.process import Direction
from Crypto.PublicKey import RSA
import argparse

# Import cryptographic utilities
from crypto_utils import (
    load_private_key,
    load_public_key,
    sign_message,
    verify_signature,
)

# ---- Cryptographic Setup ---------------------------------------------------
# Load this agent's own private key for signing outgoing messages.
CLIENT_PRIVATE_KEY = load_private_key("keys/client_private_key.pem")
CLIENT_PUBLIC_KEY_STR = load_public_key("keys/client_public_key.pem").export_key().decode('utf-8')


# ---- Frame-based Reporting Setup -------------------------------------------
FPS = 60
FRAME = -1
TIME = 0
message_buffer = asyncio.Queue()

def now():
    return time.time_ns()

def ns_to_sec(ns: int) -> float:
    return ns / 1e9

# ---- Type-Safe Data Models -------------------------------------------------
class SignedWrapper(BaseModel):
    payload: Dict[str, Any]
    signature: str
    public_key: str

class BatchReportParams(BaseModel):
    frameNumber: int
    deltaEvents: List[Dict[str, Any]]
    deltaTiming: int

class JsonRpcRequest(BaseModel):
    jsonrpc: str = "2.0"
    method: str = "batch.report"
    params: BatchReportParams
    id: Union[str, int] = Field(default_factory=lambda: str(uuid.uuid4()))

# ---- Client Initialization -------------------------------------------------
client = SummonerClient(name="ReportAgent_1")

# ---- Cryptographic Hooks ---------------------------------------------------

@client.hook(direction=Direction.SEND, priority=1)
async def sign_outgoing_message(payload: dict) -> dict:
    """Signs the payload dictionary before it's sent."""
    try:
        payload_bytes = json.dumps(payload, sort_keys=True).encode('utf-8')
        signature = sign_message(payload_bytes, CLIENT_PRIVATE_KEY)
        signed_wrapper = SignedWrapper(payload=payload, signature=signature, public_key=CLIENT_PUBLIC_KEY_STR)
        return signed_wrapper.model_dump()
    except Exception as e:
        print(f"[SIGNING ERROR] Failed to sign outgoing message: {e}")
        return None

@client.hook(direction=Direction.RECEIVE, priority=1)
async def verify_incoming_message(payload: dict) -> Union[dict, None]:
    """
    Verifies incoming messages. If the signature is valid, it passes the
    original, unmodified signed message wrapper to the receive handler.
    It silently ignores any message that is invalid or not signed.
    """
    message_to_validate = None

    if isinstance(payload, dict) and "content" in payload and "remote_addr" in payload:
        message_to_validate = payload['content']
    elif isinstance(payload, dict):
        message_to_validate = payload

    if not isinstance(message_to_validate, dict):
        return None

    try:
        signed_wrapper = SignedWrapper.model_validate(message_to_validate)
        sender_public_key = RSA.import_key(signed_wrapper.public_key.encode('utf-8'))
        original_payload_bytes = json.dumps(signed_wrapper.payload, sort_keys=True).encode('utf-8')

        if not verify_signature(original_payload_bytes, signed_wrapper.signature, sender_public_key):
            print(f"\r[AUTH ERROR] Invalid signature. Discarding.")
            return None
            
        # Signature is valid, return the original signed message for buffering
        return message_to_validate
    except ValidationError:
        return None # Silently ignore non-signed messages
    except Exception as e:
        print(f"\r[VERIFICATION ERROR] Discarding malformed message: {e}")
        return None

# ---- RPC Handlers ----------------------------------------------------------

@client.receive(route="")
async def custom_receive(msg: dict) -> None:
    """
    Handles verified signed messages that have passed through the security hook
    and buffers the entire signed object for reporting.
    """
    # The hook has already validated the message structure.
    if not isinstance(msg, dict) or "public_key" not in msg:
        return

    await message_buffer.put(msg)
    
    identity = msg['public_key']
    identity_snippet = identity.splitlines()[1][:20] + "..."
    print(f"\r[Validated and buffered signed message from {identity_snippet}]", flush=True)

async def drain_buffer() -> dict:
    """
    Drains the buffer and creates a structured batch report containing the
    original, unmodified signed messages.
    Returns a dictionary, which will then be signed by the SEND hook.
    """
    global FRAME, TIME
    
    delta_events = []
    while not message_buffer.empty():
        try:
            msg = message_buffer.get_nowait()
            delta_events.append(msg)
        except asyncio.QueueEmpty:
            break

    if not delta_events:
        await asyncio.sleep(max(0, (1 / FPS) - ns_to_sec(now() - TIME)))

    params = BatchReportParams(
        frameNumber=FRAME + 1,
        deltaEvents=delta_events,
        deltaTiming=now() - TIME,
    )

    FRAME = params.frameNumber
    TIME = now()
    
    request_model = JsonRpcRequest(params=params)
    return request_model.model_dump()

@client.send(route="")
async def custom_send() -> dict:
    """The send handler, which calls the buffer drainer."""
    return await drain_buffer()

# ---- Main Execution --------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a Summoner client with a specified config.")
    parser.add_argument('--config', dest='config_path', required=False, help='The relative path to the config file (JSON) for the client (e.g., --config configs/client_config.json)')
    args = parser.parse_args()

    try:
        TIME = time.time_ns()
        client.run(host="127.0.0.1", port=8888, config_path=args.config_path or "configs/client_config.json")
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nShutdown requested.")
    finally:
        print("Report agent shut down cleanly.")