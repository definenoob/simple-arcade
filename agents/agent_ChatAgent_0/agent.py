# main_agent.py

# ---- Section 1: Imports ----------------------------------------------------
# Import necessary libraries for asynchronous operations, unique IDs, data serialization, and random numbers.
import asyncio
import uuid
import json
import random
from typing import Union, Literal, Dict, Any, List
# Import libraries needed to handle command-line arguments and file paths.
import argparse
import os
import sys
# Import libraries for time (for cooldowns) and math (for vector calculations).
import time
from math import sqrt

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

# ---- Section 1.5: Argument Parsing for Client Identity ---------------------
# This section runs as soon as the script starts. It reads command-line arguments
# to figure out which player identity (and corresponding keys) to use.
parser = argparse.ArgumentParser(description="Run a game client with a specific identity.")
parser.add_argument('--name', type=str, required=True, help='The name of the key directory to use for this client (e.g., alice).')
args = parser.parse_args()


# ---- Section 2: Game and Pygame Global Setup -------------------------------
# Define screen dimensions for the game window.
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
# Define the size of the player's character square.
PLAYER_SIZE = 40
# Define the player's movement speed in pixels per second.
PLAYER_SPEED_PER_SECOND = 1500
# A global variable to manage the game's state, starting in the lobby.
GAME_STATE = "WAITING"  # Can be "WAITING", "ACTIVE", or "GAME_OVER"
# The global game state dictionary. It will store data for all players, keyed by their unique public key.
players = {} # e.g., { "public_key": {"x": 100, "y": 150, "color": (r,g,b), "health": 10, "last_shot_time": 0.0} }
# A state dictionary to track which movement keys are currently being held down for continuous movement.
keys_down = {"w": False, "a": False, "s": False, "d": False}

# ---- Gameplay Mechanics Setup ----
# A global list to hold all active projectiles in the game world.
projectiles = []
# Defines projectile speed in pixels per second.
PROJECTILE_SPEED_PER_SECOND = 400
# Defines the size of a projectile.
PROJECTILE_SIZE = 5
# Cooldown period for shooting, in seconds. This is the official rule of the game.
SHOOT_COOLDOWN = 0.5
# Tracks the time of the last shot to enforce the cooldown *locally* (prevents sending invalid messages).
local_last_shot_time = 0
# The maximum health each player starts with.
MAX_HEALTH = 10
# Stores the ID of the winning player when the game ends.
winner_id = None

# Initialize all the imported Pygame modules.
pygame.init()
# Create the main display surface (the game window).
screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
# Set the title of the game window.
pygame.display.set_caption(f"Multiplayer Game Client: {args.name} (Press ESC to quit)")
# Set up fonts for rendering text on the screen.
font = pygame.font.SysFont(None, 50)
small_font = pygame.font.SysFont(None, 24)
# Create an asynchronous queue to hold player actions (like moving or starting the game).
action_queue = asyncio.Queue()
# Instantiate the Summoner client, giving it a unique name to avoid conflicts with other clients.
client = SummonerClient(name=f"GameAgent_{str(uuid.uuid4())[:8]}")

# ---- Section 3: Cryptographic Setup ----------------------------------------
# Construct the path to the key files based on the provided --name argument.
key_directory = os.path.join("keys", args.name)
private_key_path = os.path.join(key_directory, "client_private_key.pem")
public_key_path = os.path.join(key_directory, "client_public_key.pem")

# Check if the required key files exist before continuing.
if not os.path.exists(private_key_path) or not os.path.exists(public_key_path):
    print(f"Error: Key files not found for identity '{args.name}' in '{key_directory}'.")
    print(f"Please generate them first by running: python login.py --name {args.name}")
    sys.exit(1) # Exit the script if keys are missing.

# Load this client's private key from its unique key file.
CLIENT_PRIVATE_KEY = load_private_key(private_key_path)
# Load this client's public key, export it as a string.
CLIENT_PUBLIC_KEY_STR = load_public_key(public_key_path).export_key().decode('utf-8')


# ---- Section 4: Type-Safe Data Models (Pydantic) ---------------------------
# Pydantic models define the expected structure of our data.

class MoveParams(BaseModel):
    direction: Literal["w", "a", "s", "d"]

class PlayerShootParams(BaseModel):
    target_x: int
    target_y: int

class GameStartParams(BaseModel):
    pass

class PlayerJoinParams(BaseModel):
    pass

class JsonRpcRequest(BaseModel):
    jsonrpc: Literal["2.0"] = "2.0"
    method: Literal["player.move", "game.start", "player.join", "player.shoot"]
    params: Union[MoveParams, GameStartParams, PlayerJoinParams, PlayerShootParams]
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

# ---- Section 5: Cryptographic Hooks ----------------------------------------
@client.hook(direction=Direction.SEND, priority=1)
async def sign_outgoing_message(payload: dict) -> dict:
    """This hook intercepts every outgoing message to add a digital signature."""
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
    """This hook intercepts every incoming message to verify its authenticity."""
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

# ---- Section 6: Core Game Logic Handlers -----------------------------------
@client.receive(route="")
async def receiver_handler(msg: dict) -> None:
    """This function handles all verified messages and updates the entire game state."""
    global GAME_STATE, projectiles, winner_id
    if not isinstance(msg, dict) or "payload" not in msg: return

    try:
        report = BatchReportRequest.model_validate(msg["payload"])
        delta_seconds = report.params.deltaTiming / 1_000_000_000.0

        if GAME_STATE == "ACTIVE":
            projectiles_to_remove = set()
            damage_map = {}

            for proj in projectiles:
                proj['x'] += proj['vx'] * delta_seconds
                proj['y'] += proj['vy'] * delta_seconds
                for p_id, p_data in players.items():
                    if p_data['health'] > 0 and p_id != proj['owner_id']:
                        player_rect = pygame.Rect(p_data['x'], p_data['y'], PLAYER_SIZE, PLAYER_SIZE)
                        if player_rect.collidepoint(proj['x'], proj['y']):
                            projectiles_to_remove.add(proj['id'])
                            damage_map[p_id] = damage_map.get(p_id, 0) + 1
                            break
                if not (0 < proj['x'] < SCREEN_WIDTH and 0 < proj['y'] < SCREEN_HEIGHT):
                    projectiles_to_remove.add(proj['id'])

            for p_id, damage in damage_map.items():
                players[p_id]['health'] = max(0, players[p_id]['health'] - damage)
            
            projectiles = [p for p in projectiles if p['id'] not in projectiles_to_remove]
        
        for event_wrapper_dict in report.params.deltaEvents:
            try:
                event_wrapper = SignedWrapper.model_validate(event_wrapper_dict)
                player_id = event_wrapper.public_key
                request = JsonRpcRequest.model_validate(event_wrapper.payload)

                if request.method == "game.start":
                    if GAME_STATE == "WAITING": GAME_STATE = "ACTIVE"
                    continue

                if request.method in ["player.join", "player.move"]:
                    if player_id not in players:
                        random.seed(player_id)
                        players[player_id] = {
                            "x": random.randint(0, SCREEN_WIDTH - PLAYER_SIZE),
                            "y": random.randint(0, SCREEN_HEIGHT - PLAYER_SIZE),
                            "color": (random.randint(50, 255), random.randint(50, 255), random.randint(50, 255)),
                            "health": MAX_HEALTH,
                            "last_shot_time": 0.0
                        }
                        random.seed()
                        asyncio.create_task(action_queue.put({"type": "join"}))

                if GAME_STATE == "ACTIVE" and player_id in players and players[player_id]['health'] > 0:
                    if request.method == "player.move":
                        # ---- THE FIX IS HERE ----
                        # Use the direction from the network message, NOT the local keys_down state.
                        # This ensures all clients calculate the same movement for every player.
                        move_distance = PLAYER_SPEED_PER_SECOND * delta_seconds
                        player = players[player_id]
                        direction = request.params.direction
                        if direction == "w": player["y"] -= move_distance
                        elif direction == "s": player["y"] += move_distance
                        elif direction == "a": player["x"] -= move_distance
                        elif direction == "d": player["x"] += move_distance
                        player["x"] = max(0, min(player["x"], SCREEN_WIDTH - PLAYER_SIZE))
                        player["y"] = max(0, min(player["y"], SCREEN_HEIGHT - PLAYER_SIZE))

                    elif request.method == "player.shoot":
                        current_time = time.time()
                        player_state = players[player_id]
                        if (current_time - player_state.get('last_shot_time', 0)) > SHOOT_COOLDOWN:
                            player_state['last_shot_time'] = current_time
                            start_x = player_state['x'] + PLAYER_SIZE / 2
                            start_y = player_state['y'] + PLAYER_SIZE / 2
                            dir_x = request.params.target_x - start_x
                            dir_y = request.params.target_y - start_y
                            length = sqrt(dir_x**2 + dir_y**2)
                            if length > 0:
                                vx = (dir_x / length) * PROJECTILE_SPEED_PER_SECOND
                                vy = (dir_y / length) * PROJECTILE_SPEED_PER_SECOND
                                projectiles.append({
                                    "x": start_x, "y": start_y, "vx": vx, "vy": vy,
                                    "owner_id": player_id, "id": str(uuid.uuid4())
                                })
            except (ValidationError, KeyError):
                continue
        
        if GAME_STATE == "ACTIVE":
            active_players = [pid for pid, pdata in players.items() if pdata['health'] > 0]
            if len(active_players) <= 1:
                GAME_STATE = "GAME_OVER"
                winner_id = active_players[0] if active_players else None

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
    elif action["type"] == "shoot":
        params = PlayerShootParams(target_x=action["target"][0], target_y=action["target"][1])
        request_model = JsonRpcRequest(method="player.shoot", params=params)
    return request_model.model_dump()

# ---- Section 7: Main Application Loop --------------------------------------
async def game_loop():
    global local_last_shot_time
    running = True
    while running:
        try:
            our_player_alive = CLIENT_PUBLIC_KEY_STR in players and players[CLIENT_PUBLIC_KEY_STR]['health'] > 0
            
            for event in pygame.event.get():
                if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                    running = False
                if event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE and GAME_STATE == "WAITING":
                    await action_queue.put({"type": "start"})
                
                if GAME_STATE == "ACTIVE" and our_player_alive:
                    if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                        current_time = time.time()
                        if (current_time - local_last_shot_time) > SHOOT_COOLDOWN:
                            local_last_shot_time = current_time
                            await action_queue.put({"type": "shoot", "target": event.pos})
                    
                    if event.type == pygame.KEYDOWN:
                        key_name = pygame.key.name(event.key)
                        if key_name in keys_down:
                            keys_down[key_name] = True
                    elif event.type == pygame.KEYUP:
                        key_name = pygame.key.name(event.key)
                        if key_name in keys_down:
                            keys_down[key_name] = False

            if GAME_STATE == "ACTIVE" and our_player_alive:
                for key, pressed in keys_down.items():
                    if pressed:
                        await action_queue.put({"type": "move", "dir": key})

            screen.fill((25, 25, 35))
            if GAME_STATE == "WAITING":
                for player_id, data in players.items():
                    pygame.draw.rect(screen, data["color"], pygame.Rect(data["x"], data["y"], PLAYER_SIZE, PLAYER_SIZE))
                count_text = font.render(f"{len(players)} Players Connected", True, (255, 255, 255))
                start_text = font.render("Press SPACE to Start", True, (200, 200, 200))
                screen.blit(count_text, (SCREEN_WIDTH/2 - count_text.get_width()/2, 40))
                screen.blit(start_text, (SCREEN_WIDTH/2 - start_text.get_width()/2, 90))
            
            elif GAME_STATE == "ACTIVE" or GAME_STATE == "GAME_OVER":
                for p_id, p_data in players.items():
                    if p_data['health'] > 0:
                        pygame.draw.rect(screen, (100, 0, 0), pygame.Rect(p_data['x'], p_data['y'] - 15, PLAYER_SIZE, 10))
                        health_width = (p_data['health'] / MAX_HEALTH) * PLAYER_SIZE
                        pygame.draw.rect(screen, (0, 200, 0), pygame.Rect(p_data['x'], p_data['y'] - 15, health_width, 10))
                        pygame.draw.rect(screen, p_data["color"], pygame.Rect(p_data['x'], p_data['y'], PLAYER_SIZE, PLAYER_SIZE))
                
                for proj in projectiles:
                    pygame.draw.circle(screen, (255, 255, 100), (proj['x'], proj['y']), PROJECTILE_SIZE)

                if GAME_STATE == "GAME_OVER":
                    over_text = font.render("GAME OVER", True, (255, 0, 0))
                    screen.blit(over_text, (SCREEN_WIDTH/2 - over_text.get_width()/2, SCREEN_HEIGHT/2 - 50))
                    if winner_id and winner_id in players:
                        winner_snippet = winner_id.splitlines()[-2][-10:]
                        winner_text = font.render(f"Winner: {winner_snippet}", True, players[winner_id]['color'])
                        screen.blit(winner_text, (SCREEN_WIDTH/2 - winner_text.get_width()/2, SCREEN_HEIGHT/2 + 10))

            pygame.display.flip()
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
        loop = asyncio.get_event_loop()
        loop.create_task(game_loop())
        loop.call_soon(lambda: asyncio.create_task(action_queue.put({"type": "join"})))
        client.run(host="127.0.0.1", port=8888)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("Shutdown requested.")
    finally:
        if pygame.get_init(): pygame.quit()
        print("Client and Pygame shut down cleanly.")