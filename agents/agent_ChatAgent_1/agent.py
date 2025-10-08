import asyncio
import uuid
import json
import random
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

# ---- Game and Pygame Setup -------------------------------------------------
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
PLAYER_SIZE = 40
PLAYER_SPEED = 10
GAME_STATE = "WAITING"  # Can be "WAITING" or "ACTIVE"
players = {} # Global state: { "public_key": {"x": int, "y": int, "color": tuple} }
keys_down = {"w": False, "a": False, "s": False, "d": False} # State for continuous movement

pygame.init()
screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
pygame.display.set_caption("Multiplayer Game Client (Press ESC to quit)")
font = pygame.font.SysFont(None, 50)
small_font = pygame.font.SysFont(None, 24)
action_queue = asyncio.Queue()
client = SummonerClient(name=f"GameAgent_{str(uuid.uuid4())[:8]}")

# ---- Cryptographic Setup ---------------------------------------------------
CLIENT_PRIVATE_KEY = load_private_key("keys/client_private_key.pem")
CLIENT_PUBLIC_KEY_STR = load_public_key("keys/client_public_key.pem").export_key().decode('utf-8')

# ---- Type-Safe JSON-RPC 2.0 Data Models ------------------------------------
class MoveParams(BaseModel):
    direction: Literal["w", "a", "s", "d"]

class GameStartParams(BaseModel):
    pass

class PlayerJoinParams(BaseModel):
    pass

class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["player.move", "game.start", "player.join"]
    params: Union[MoveParams, GameStartParams, PlayerJoinParams]
    id: Union[str, int] = Field(default_factory=lambda: str(uuid.uuid4()))

class BatchReportParams(BaseModel):
    frameNumber: int
    deltaEvents: List[Dict[str, Any]]
    deltaTiming: int

class BatchReportRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["batch.report"]
    params: BatchReportParams
    id: Union[str, int]

class SignedWrapper(BaseModel):
    payload: Dict[str, Any]
    signature: str
    public_key: str

# ---- Cryptographic Hooks (Unchanged) ---------------------------------------
@client.hook(direction=Direction.SEND, priority=1)
async def sign_outgoing_message(payload: dict) -> dict:
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
    message_to_validate = payload.get('content') if isinstance(payload, dict) and 'content' in payload else payload
    if not isinstance(message_to_validate, dict): return None
    try:
        signed_wrapper = SignedWrapper.model_validate(message_to_validate)
        sender_public_key = RSA.import_key(signed_wrapper.public_key.encode('utf-8'))
        original_payload_bytes = json.dumps(signed_wrapper.payload, sort_keys=True).encode('utf-8')
        if not verify_signature(original_payload_bytes, signed_wrapper.signature, sender_public_key):
            return None
        return {"identity": signed_wrapper.public_key, "payload": signed_wrapper.payload}
    except (ValidationError, Exception):
        return None

# ---- Game Logic and RPC Handlers -------------------------------------------
@client.receive(route="")
async def receiver_handler(msg: dict) -> None:
    global GAME_STATE
    if not isinstance(msg, dict) or "payload" not in msg: return

    try:
        report = BatchReportRequest.model_validate(msg["payload"])
        for event_wrapper_dict in report.params.deltaEvents:
            try:
                event_wrapper = SignedWrapper.model_validate(event_wrapper_dict)
                player_id = event_wrapper.public_key
                request = JsonRpcRequest.model_validate(event_wrapper.payload)

                if request.method == "game.start":
                    GAME_STATE = "ACTIVE"
                    continue

                if request.method in ["player.join", "player.move"]:
                    is_new_player = player_id not in players
                    
                    if is_new_player:
                        random.seed(player_id)
                        players[player_id] = {
                            "x": random.randint(0, SCREEN_WIDTH - PLAYER_SIZE),
                            "y": random.randint(0, SCREEN_HEIGHT - PLAYER_SIZE),
                            "color": (random.randint(50, 255), random.randint(50, 255), random.randint(50, 255))
                        }
                        random.seed()
                        # If we just discovered a new player, respond by re-broadcasting our own presence.
                        asyncio.create_task(action_queue.put({"type": "join"}))

                if GAME_STATE == "ACTIVE" and request.method == "player.move":
                    player = players[player_id]
                    direction = request.params.direction
                    if direction == "w": player["y"] -= PLAYER_SPEED
                    elif direction == "s": player["y"] += PLAYER_SPEED
                    elif direction == "a": player["x"] -= PLAYER_SPEED
                    elif direction == "d": player["x"] += PLAYER_SPEED
                    player["x"] = max(0, min(player["x"], SCREEN_WIDTH - PLAYER_SIZE))
                    player["y"] = max(0, min(player["y"], SCREEN_HEIGHT - PLAYER_SIZE))
            except ValidationError:
                continue
    except ValidationError:
        return

@client.send(route="")
async def send_handler() -> dict:
    action = await action_queue.get()
    request_model = None
    if action["type"] == "join":
        request_model = JsonRpcRequest(method="player.join", params=PlayerJoinParams())
    elif action["type"] == "start":
        request_model = JsonRpcRequest(method="game.start", params=GameStartParams())
    elif action["type"] == "move":
        params = MoveParams(direction=action["dir"])
        request_model = JsonRpcRequest(method="player.move", params=params)
    return request_model.model_dump()

# ---- Main Application Loop -------------------------------------------------
async def game_loop():
    running = True
    while running:
        try:
            # Event handling for key presses and releases
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                    running = False
                
                if event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE and GAME_STATE == "WAITING":
                    await action_queue.put({"type": "start"})

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

            # Send movement commands based on which keys are held down
            if GAME_STATE == "ACTIVE":
                if keys_down["w"]: await action_queue.put({"type": "move", "dir": "w"})
                if keys_down["a"]: await action_queue.put({"type": "move", "dir": "a"})
                if keys_down["s"]: await action_queue.put({"type": "move", "dir": "s"})
                if keys_down["d"]: await action_queue.put({"type": "move", "dir": "d"})

            # Rendering logic
            screen.fill((25, 25, 35))
            if GAME_STATE == "WAITING":
                for player_id, data in players.items():
                    rect = pygame.Rect(data["x"], data["y"], PLAYER_SIZE, PLAYER_SIZE)
                    pygame.draw.rect(screen, data["color"], rect)
                
                count_text = font.render(f"{len(players)} Players Connected", True, (255, 255, 255))
                start_text = font.render("Press SPACE to Start", True, (200, 200, 200))
                screen.blit(count_text, (SCREEN_WIDTH/2 - count_text.get_width()/2, 40))
                screen.blit(start_text, (SCREEN_WIDTH/2 - start_text.get_width()/2, 90))

            elif GAME_STATE == "ACTIVE":
                for player_id, data in players.items():
                    rect = pygame.Rect(data["x"], data["y"], PLAYER_SIZE, PLAYER_SIZE)
                    pygame.draw.rect(screen, data["color"], rect)
                    id_snippet = player_id.splitlines()[-2][-10:]
                    text_surface = small_font.render(id_snippet, True, (255, 255, 255))
                    screen.blit(text_surface, (data["x"], data["y"] - 20))

            pygame.display.flip()
            await asyncio.sleep(0.016)
        except asyncio.CancelledError:
            running = False

    print("Shutdown initiated...")
    current_task = asyncio.current_task()
    for task in asyncio.all_tasks():
        if task is not current_task: task.cancel()

if __name__ == "__main__":
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(game_loop())
        # Immediately send an initial 'join' message on startup
        loop.call_soon(lambda: asyncio.create_task(action_queue.put({"type": "join"})))
        client.run(host="127.0.0.1", port=8888)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Shutdown requested.")
    finally:
        if pygame.get_init(): pygame.quit()
        print("Client and Pygame shut down cleanly.")