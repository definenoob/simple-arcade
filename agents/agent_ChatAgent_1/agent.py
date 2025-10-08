# main_agent.py

# ---- Section 1: Imports ----------------------------------------------------
# Import necessary libraries for asynchronous operations, unique IDs, data serialization, and random numbers.
import asyncio
import uuid
import json
import random
from typing import Union, Literal, Dict, Any, List

# Import Pygame for graphics, windowing, and input handling.
import pygame
# Import Pydantic for creating type-safe data models. This helps validate incoming and outgoing data.
from pydantic import BaseModel, Field, ValidationError
# Import the Summoner client and protocol definitions.
from summoner.client import SummonerClient
from summoner.protocol.process import Direction
# Import the PyCryptodome library for cryptographic operations like signing and verification.
from Crypto.PublicKey import RSA

# Import our custom cryptographic utility functions from a separate file.
from crypto_utils import (
    load_private_key,
    load_public_key,
    sign_message,
    verify_signature,
)

# ---- Section 2: Game and Pygame Global Setup -------------------------------
# Define screen dimensions for the game window.
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
# Define the size of the player's character square.
PLAYER_SIZE = 40
# Define the player's movement speed in pixels per second.
# This ensures movement is consistent regardless of the frame rate.
PLAYER_SPEED_PER_SECOND = 1500
# A global variable to manage the game's state, starting in the lobby.
GAME_STATE = "WAITING"  # Can be "WAITING" or "ACTIVE"
# The global game state dictionary. It will store data for all players, keyed by their unique public key.
players = {} # e.g., { "public_key_of_player1": {"x": 100, "y": 150, "color": (r,g,b)} }
# A state dictionary to track which movement keys are currently being held down for continuous movement.
keys_down = {"w": False, "a": False, "s": False, "d": False}

# Initialize all the imported Pygame modules.
pygame.init()
# Create the main display surface (the game window).
screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
# Set the title of the game window.
pygame.display.set_caption("Multiplayer Game Client (Press ESC to quit)")
# Set up fonts for rendering text on the screen.
font = pygame.font.SysFont(None, 50)
small_font = pygame.font.SysFont(None, 24)
# Create an asynchronous queue to hold player actions (like moving or starting the game).
# This decouples input handling from message sending.
action_queue = asyncio.Queue()
# Instantiate the Summoner client, giving it a unique name to avoid conflicts with other clients.
client = SummonerClient(name=f"GameAgent_{str(uuid.uuid4())[:8]}")

# ---- Section 3: Cryptographic Setup ----------------------------------------
# Load this client's private key from its key file. This is used for *signing* outgoing messages.
CLIENT_PRIVATE_KEY = load_private_key("keys/client_private_key.pem")
# Load this client's public key, export it as a string. This is sent with messages so others can *verify* them.
CLIENT_PUBLIC_KEY_STR = load_public_key("keys/client_public_key.pem").export_key().decode('utf-8')

# ---- Section 4: Type-Safe Data Models (Pydantic) ---------------------------
# Pydantic models define the expected structure of our data.
# If data doesn't match the model, a validation error is raised, preventing malformed data.

# Defines the parameters for a player movement action.
class MoveParams(BaseModel):
    direction: Literal["w", "a", "s", "d"]

# Defines the (empty) parameters for a "start the game" action.
class GameStartParams(BaseModel):
    pass

# Defines the (empty) parameters for a "player joining" action.
class PlayerJoinParams(BaseModel):
    pass

# The main JSON-RPC 2.0 request model. It defines the structure for all our game actions.
class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["player.move", "game.start", "player.join"]
    params: Union[MoveParams, GameStartParams, PlayerJoinParams]
    id: Union[str, int] = Field(default_factory=lambda: str(uuid.uuid4()))

# This model defines the structure of the batch reports we expect to receive *from* the ReportAgent.
class BatchReportParams(BaseModel):
    frameNumber: int
    deltaEvents: List[Dict[str, Any]]
    deltaTiming: int

class BatchReportRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["batch.report"]
    params: BatchReportParams
    id: Union[str, int]

# A security wrapper for our messages. All outgoing messages will be placed inside this structure.
class SignedWrapper(BaseModel):
    payload: Dict[str, Any]  # The original message (e.g., a JsonRpcRequest)
    signature: str           # The digital signature of the payload
    public_key: str          # The public key of the sender, so the receiver can verify the signature

# ---- Section 5: Cryptographic Hooks ----------------------------------------
# Hooks are functions that intercept messages before they are sent or after they are received.

@client.hook(direction=Direction.SEND, priority=1)
async def sign_outgoing_message(payload: dict) -> dict:
    """This hook intercepts every outgoing message to add a digital signature."""
    try:
        # The payload must be converted to a consistent string format before signing.
        payload_bytes = json.dumps(payload, sort_keys=True).encode('utf-8')
        # Sign the message bytes with our private key.
        signature = sign_message(payload_bytes, CLIENT_PRIVATE_KEY)
        # Wrap the original payload, the new signature, and our public key into the SignedWrapper model.
        signed_wrapper = SignedWrapper(payload=payload, signature=signature, public_key=CLIENT_PUBLIC_KEY_STR)
        # Return the dictionary version of the wrapper, which will be sent over the network.
        return signed_wrapper.model_dump()
    except Exception as e:
        print(f"[SIGNING ERROR] Failed to sign outgoing message: {e}")
        return None # Returning None stops the message from being sent.

@client.hook(direction=Direction.RECEIVE, priority=1)
async def verify_incoming_message(payload: dict) -> Union[dict, None]:
    """This hook intercepts every incoming message to verify its authenticity."""
    # The server might wrap the message; this extracts the actual content.
    message_to_validate = payload.get('content') if isinstance(payload, dict) and 'content' in payload else payload
    if not isinstance(message_to_validate, dict): return None
    try:
        # Validate that the incoming message conforms to our SignedWrapper structure.
        signed_wrapper = SignedWrapper.model_validate(message_to_validate)
        # Import the sender's public key (which they included in the message).
        sender_public_key = RSA.import_key(signed_wrapper.public_key.encode('utf-8'))
        # Recreate the exact original message string that the sender signed.
        original_payload_bytes = json.dumps(signed_wrapper.payload, sort_keys=True).encode('utf-8')
        # Verify the signature against the payload using the sender's public key.
        if not verify_signature(original_payload_bytes, signed_wrapper.signature, sender_public_key):
            return None # If the signature is invalid, discard the message.
        # If valid, pass a clean dictionary with the sender's identity and the verified payload to the receiver.
        return {"identity": signed_wrapper.public_key, "payload": signed_wrapper.payload}
    except (ValidationError, Exception):
        # If the message isn't a SignedWrapper or something else goes wrong, discard it.
        return None

# ---- Section 6: Core Game Logic Handlers -----------------------------------
@client.receive(route="")
async def receiver_handler(msg: dict) -> None:
    """This function handles all verified messages and updates the game state."""
    global GAME_STATE # We need to modify the global game state
    if not isinstance(msg, dict) or "payload" not in msg: return

    try:
        # First, validate that the message is a batch report from the ReportAgent.
        report = BatchReportRequest.model_validate(msg["payload"])
        # Extract the delta time from the report and convert it from nanoseconds to seconds.
        # This is the authoritative "tick" of our game clock.
        delta_seconds = report.params.deltaTiming / 1_000_000_000.0

        # Process each individual event that the reporter collected in this batch.
        for event_wrapper_dict in report.params.deltaEvents:
            try:
                # Validate the inner event as a SignedWrapper from another player.
                event_wrapper = SignedWrapper.model_validate(event_wrapper_dict)
                player_id = event_wrapper.public_key # The unique ID of the player who sent the event.
                request = JsonRpcRequest.model_validate(event_wrapper.payload)

                # --- State Change Logic ---
                if request.method == "game.start":
                    GAME_STATE = "ACTIVE"
                    continue # Nothing more to do for this event.

                if request.method in ["player.join", "player.move"]:
                    is_new_player = player_id not in players
                    # If this is the first time we're hearing from this player...
                    if is_new_player:
                        # Use their public key to seed the random number generator.
                        # This guarantees that every client will generate the exact same starting
                        # position and color for this new player, preventing desynchronization.
                        random.seed(player_id)
                        players[player_id] = {
                            "x": random.randint(0, SCREEN_WIDTH - PLAYER_SIZE),
                            "y": random.randint(0, SCREEN_HEIGHT - PLAYER_SIZE),
                            "color": (random.randint(50, 255), random.randint(50, 255), random.randint(50, 255))
                        }
                        random.seed() # Reset the seed so other random numbers are not deterministic.
                        
                        # "Call and Response": If we just discovered a new player,
                        # we immediately re-broadcast our own presence. This ensures the
                        # new player learns about us right away.
                        asyncio.create_task(action_queue.put({"type": "join"}))

                # --- Player Movement Logic ---
                # Only update positions if the game is active and it's a move event.
                if GAME_STATE == "ACTIVE" and request.method == "player.move":
                    # Calculate the distance to move based on our speed and the authoritative delta time.
                    # This makes movement smooth and independent of frame rate or network lag.
                    move_distance = PLAYER_SPEED_PER_SECOND * delta_seconds
                    
                    player = players[player_id]
                    direction = request.params.direction
                    if direction == "w": player["y"] -= move_distance
                    elif direction == "s": player["y"] += move_distance
                    elif direction == "a": player["x"] -= move_distance
                    elif direction == "d": player["x"] += move_distance
                    # Ensure players don't move off-screen.
                    player["x"] = max(0, min(player["x"], SCREEN_WIDTH - PLAYER_SIZE))
                    player["y"] = max(0, min(player["y"], SCREEN_HEIGHT - PLAYER_SIZE))
            except ValidationError:
                # Ignore any individual event in the batch that is malformed.
                continue
    except ValidationError:
        # Ignore any incoming message that is not a valid batch report.
        return

@client.send(route="")
async def send_handler() -> dict:
    """This function waits for actions on the queue and sends them to the server."""
    # This will wait until an action is put on the action_queue by the game_loop.
    action = await action_queue.get()
    request_model = None
    # Construct the correct Pydantic model based on the action type.
    if action["type"] == "join":
        request_model = JsonRpcRequest(method="player.join", params=PlayerJoinParams())
    elif action["type"] == "start":
        request_model = JsonRpcRequest(method="game.start", params=GameStartParams())
    elif action["type"] == "move":
        params = MoveParams(direction=action["dir"])
        request_model = JsonRpcRequest(method="player.move", params=params)
    # Convert the model to a dictionary to be sent. The 'sign_outgoing_message' hook will intercept this.
    return request_model.model_dump()

# ---- Section 7: Main Application Loop --------------------------------------
async def game_loop():
    """The main loop that handles input, rendering, and game logic ticks."""
    running = True
    while running:
        try:
            # --- Input Handling ---
            # Process all events in Pygame's event queue.
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                    running = False
                # The SPACE key only works in the WAITING state to start the game.
                if event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE and GAME_STATE == "WAITING":
                    await action_queue.put({"type": "start"})
                
                # WASD input is only processed when the game is ACTIVE.
                if GAME_STATE == "ACTIVE":
                    if event.type == pygame.KEYDOWN:
                        if event.key == pygame.K_w: keys_down["w"] = True
                        elif event.key == pygame.K_a: keys_down["a"] = True
                        elif event.key == pygame.K_s: keys_down["s"] = True
                        elif event.key == pygame.K_d: keys_down["d"] = True
                    elif event.type == pygame.KEYUP:
                        if event.key == pygame.K_w: keys_down["w"] = False
                        elif event.key == pygame.K_a: keys_down["a"] = False
                        elif event.key == pygame.K_s: keys_down["s"] = False
                        elif event.key == pygame.K_d: keys_down["d"] = False

            # --- Action Generation for Continuous Movement ---
            # In every frame, if a key is held down, put a move action on the queue.
            if GAME_STATE == "ACTIVE":
                if keys_down["w"]: await action_queue.put({"type": "move", "dir": "w"})
                if keys_down["a"]: await action_queue.put({"type": "move", "dir": "a"})
                if keys_down["s"]: await action_queue.put({"type": "move", "dir": "s"})
                if keys_down["d"]: await action_queue.put({"type": "move", "dir": "d"})

            # --- Rendering ---
            # Clear the screen with a dark background.
            screen.fill((25, 25, 35))
            # Render the appropriate scene based on the current game state.
            if GAME_STATE == "WAITING":
                # Draw players that have already joined the lobby.
                for player_id, data in players.items():
                    rect = pygame.Rect(data["x"], data["y"], PLAYER_SIZE, PLAYER_SIZE)
                    pygame.draw.rect(screen, data["color"], rect)
                # Display lobby text.
                count_text = font.render(f"{len(players)} Players Connected", True, (255, 255, 255))
                start_text = font.render("Press SPACE to Start", True, (200, 200, 200))
                screen.blit(count_text, (SCREEN_WIDTH/2 - count_text.get_width()/2, 40))
                screen.blit(start_text, (SCREEN_WIDTH/2 - start_text.get_width()/2, 90))
            elif GAME_STATE == "ACTIVE":
                # Draw all players at their current positions.
                for player_id, data in players.items():
                    rect = pygame.Rect(data["x"], data["y"], PLAYER_SIZE, PLAYER_SIZE)
                    pygame.draw.rect(screen, data["color"], rect)
                    # Use the last characters of the public key's last line for the name.
                    id_snippet = player_id.splitlines()[-2][-10:]
                    text_surface = small_font.render(id_snippet, True, (255, 255, 255))
                    screen.blit(text_surface, (data["x"], data["y"] - 20))

            # Update the full display surface to the screen.
            pygame.display.flip()
            # CRITICAL: Yield control to the asyncio event loop. This allows the background
            # networking tasks (like receiving messages) to run. Without this, the
            # game loop would block everything and no messages would ever be received.
            await asyncio.sleep(0.016) # Aim for ~60 FPS
        except asyncio.CancelledError:
            running = False

    print("Shutdown initiated...")
    current_task = asyncio.current_task()
    for task in asyncio.all_tasks():
        if task is not current_task: task.cancel()

# ---- Section 8: Main Execution Block ---------------------------------------
if __name__ == "__main__":
    try:
        # Get the main asyncio event loop.
        loop = asyncio.get_event_loop()
        # Start the game loop as a background task.
        loop.create_task(game_loop())
        # Immediately send an initial 'join' message to announce our presence to the network.
        loop.call_soon(lambda: asyncio.create_task(action_queue.put({"type": "join"})))
        # Start the Summoner client. This is a blocking call that runs the networking loop
        # until the program is interrupted.
        client.run(host="127.0.0.1", port=8888)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Shutdown requested.")
    finally:
        # Cleanly shut down Pygame when the client is closed.
        if pygame.get_init(): pygame.quit()
        print("Client and Pygame shut down cleanly.")