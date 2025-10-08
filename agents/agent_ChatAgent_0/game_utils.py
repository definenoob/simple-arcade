# game_utils.py
# Encapsulates the game engine, including state management, logic, rendering, and input.

import pygame
import random
import time
import uuid
import asyncio
from math import sqrt
from typing import Dict, Any, List
from pydantic import ValidationError

# Import the data models
try:
    from models import JsonRpcRequest, SignedWrapper, BatchReportParams
except ImportError:
    print("Error: models.py not found.")
    exit(1)

# ---- Game Constants --------------------------------------------------------
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
PLAYER_SIZE = 40
# Speeds are defined per second for frame-rate independence.
PLAYER_SPEED_PER_SECOND = 1500
PROJECTILE_SPEED_PER_SECOND = 800
PROJECTILE_SIZE = 5
# Cooldown period for shooting (official game rule).
SHOOT_COOLDOWN = 0.25
MAX_HEALTH = 10

# ---- GameEngine Class ------------------------------------------------------
class GameEngine:
    """Manages the entire game state, logic, input handling, and rendering."""
    def __init__(self, player_name: str, client_public_key: str):
        # Identity of the local client
        self.player_name = player_name
        self.client_public_key = client_public_key

        # Core game state
        self.GAME_STATE = "WAITING"  # "WAITING", "ACTIVE", "GAME_OVER"
        self.players = {} # { "public_key": {"x", "y", "color", "health", "last_shot_time"} }
        self.projectiles = []
        self.winner_id = None

        # Local input state tracking
        self.keys_down = {"w": False, "a": False, "s": False, "d": False}
        self.local_last_shot_time = 0 # Used for client-side input gating

        # Initialize Pygame
        pygame.init()
        self.screen = pygame.display.set_mode((SCREEN_WIDTH, SCREEN_HEIGHT))
        pygame.display.set_caption(f"Multiplayer Game Client: {self.player_name} (Press ESC to quit)")
        self.font = pygame.font.SysFont(None, 50)
        self.small_font = pygame.font.SysFont(None, 24)

    def shutdown(self):
        """Cleanly shuts down the Pygame instance."""
        if pygame.get_init():
            pygame.quit()
            print("Pygame shut down cleanly.")

    # ---- Core Update Logic (The Game Tick) ---------------------------------
    def process_report(self, report_params: BatchReportParams, action_queue: asyncio.Queue):
        """
        The main game tick. Processes the authoritative batch report and updates the game state.
        """
        # The report's `deltaTiming` is the authoritative clock. Convert nanoseconds to seconds.
        delta_seconds = report_params.deltaTiming / 1_000_000_000.0

        # --- Tick Part 1: Update existing simulation objects ---
        if self.GAME_STATE == "ACTIVE":
            self._update_simulation(delta_seconds)

        # --- Tick Part 2: Process new events from the report ---
        self._process_events(report_params.deltaEvents, delta_seconds, action_queue)

        # --- Tick Part 3: Check for a Win Condition ---
        if self.GAME_STATE == "ACTIVE":
            self._check_win_condition()

    def _update_simulation(self, delta_seconds: float):
        """Handles projectile movement, collision detection, and damage application."""
        projectiles_to_remove = set()
        damage_map = {} # Stores damage dealt this frame: {player_id: damage}

        for proj in self.projectiles:
            proj['x'] += proj['vx'] * delta_seconds
            proj['y'] += proj['vy'] * delta_seconds

            # Check for collisions with players.
            for p_id, p_data in self.players.items():
                # Players must be alive and cannot shoot themselves.
                if p_data['health'] > 0 and p_id != proj['owner_id']:
                    player_rect = pygame.Rect(p_data['x'], p_data['y'], PLAYER_SIZE, PLAYER_SIZE)
                    if player_rect.collidepoint(proj['x'], proj['y']):
                        projectiles_to_remove.add(proj['id'])
                        damage_map[p_id] = damage_map.get(p_id, 0) + 1 # 1 damage per hit
                        break # A projectile can only hit one player

            # Mark projectiles that have gone off-screen.
            if not (0 < proj['x'] < SCREEN_WIDTH and 0 < proj['y'] < SCREEN_HEIGHT):
                projectiles_to_remove.add(proj['id'])

        # Apply all damage calculated during this frame.
        for p_id, damage in damage_map.items():
            if p_id in self.players:
                self.players[p_id]['health'] = max(0, self.players[p_id]['health'] - damage)

        # Rebuild the projectiles list, excluding those marked for removal.
        self.projectiles = [p for p in self.projectiles if p['id'] not in projectiles_to_remove]

    def _process_events(self, events: List[Dict[str, Any]], delta_seconds: float, action_queue: asyncio.Queue):
        """Interprets the actions included in the report and applies them to the state."""
        for event_wrapper_dict in events:
            try:
                # Validate the wrapper and the request structure.
                # Note: The main agent verifies the signatures of the outer report.
                event_wrapper = SignedWrapper.model_validate(event_wrapper_dict)
                player_id = event_wrapper.public_key
                request = JsonRpcRequest.model_validate(event_wrapper.payload)

                if request.method == "game.start":
                    if self.GAME_STATE == "WAITING":
                        self.GAME_STATE = "ACTIVE"
                    continue

                # Handle player initialization (on join or first move).
                if request.method in ["player.join", "player.move"]:
                    if player_id not in self.players:
                        self._add_new_player(player_id)
                        # If we see another player, ensure we have also announced ourselves.
                        asyncio.create_task(action_queue.put({"type": "join"}))

                # Process gameplay actions (only if active and the player is alive).
                if self.GAME_STATE == "ACTIVE" and player_id in self.players and self.players[player_id]['health'] > 0:
                    if request.method == "player.move":
                        self._move_player(player_id, request.params.direction, delta_seconds)
                    elif request.method == "player.shoot":
                        self._handle_shot(player_id, request.params.target_x, request.params.target_y)

            except (ValidationError, KeyError):
                # Ignore malformed individual events within the batch.
                continue

    def _add_new_player(self, player_id: str):
        """Initializes a new player with deterministic properties."""
        random.seed(player_id) # Seed based on ID for consistency across clients
        self.players[player_id] = {
            "x": random.randint(0, SCREEN_WIDTH - PLAYER_SIZE),
            "y": random.randint(0, SCREEN_HEIGHT - PLAYER_SIZE),
            "color": (random.randint(50, 255), random.randint(50, 255), random.randint(50, 255)),
            "health": MAX_HEALTH,
            "last_shot_time": 0.0
        }
        random.seed() # Reset seed

    def _move_player(self, player_id: str, direction: str, delta_seconds: float):
        """Applies authoritative movement based on the network report."""
        move_distance = PLAYER_SPEED_PER_SECOND * delta_seconds
        player = self.players[player_id]
        if direction == "w": player["y"] -= move_distance
        elif direction == "s": player["y"] += move_distance
        elif direction == "a": player["x"] -= move_distance
        elif direction == "d": player["x"] += move_distance
        # Constrain movement to the screen boundaries
        player["x"] = max(0, min(player["x"], SCREEN_WIDTH - PLAYER_SIZE))
        player["y"] = max(0, min(player["y"], SCREEN_HEIGHT - PLAYER_SIZE))

    def _handle_shot(self, player_id: str, target_x: int, target_y: int):
        """Validates a shot action and creates a projectile if valid."""
        # PEER-SIDE ENFORCEMENT: Validate the cooldown.
        current_time = time.time()
        player_state = self.players[player_id]
        if (current_time - player_state.get('last_shot_time', 0)) > SHOOT_COOLDOWN:
            player_state['last_shot_time'] = current_time # Update authoritative shot time

            # Calculate projectile trajectory
            start_x = player_state['x'] + PLAYER_SIZE / 2
            start_y = player_state['y'] + PLAYER_SIZE / 2
            dir_x = target_x - start_x
            dir_y = target_y - start_y
            length = sqrt(dir_x**2 + dir_y**2)
            if length > 0: # Avoid division by zero
                vx = (dir_x / length) * PROJECTILE_SPEED_PER_SECOND
                vy = (dir_y / length) * PROJECTILE_SPEED_PER_SECOND
                self.projectiles.append({
                    "x": start_x, "y": start_y, "vx": vx, "vy": vy,
                    "owner_id": player_id, "id": str(uuid.uuid4())
                })

    def _check_win_condition(self):
        """Checks if the game has ended."""
        active_players = [pid for pid, pdata in self.players.items() if pdata['health'] > 0]
        # Game ends if 1 or 0 left. We check len(self.players) > 0 to ensure game doesn't end instantly if empty.
        if len(active_players) <= 1 and len(self.players) > 0:
            self.GAME_STATE = "GAME_OVER"
            self.winner_id = active_players[0] if active_players else None

    # ---- Input Handling ----------------------------------------------------
    def handle_input(self, action_queue: asyncio.Queue) -> bool:
        """Handles Pygame events and adds corresponding actions to the queue. Returns False on quit."""
        # Determine if the local player is currently alive.
        our_player_alive = self.client_public_key in self.players and self.players[self.client_public_key]['health'] > 0
        running = True

        for event in pygame.event.get():
            if event.type == pygame.QUIT or (event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE):
                running = False
                return running

            # Start game input
            if event.type == pygame.KEYDOWN and event.key == pygame.K_SPACE and self.GAME_STATE == "WAITING":
                # Use asyncio.create_task to safely put items on the queue from sync context
                asyncio.create_task(action_queue.put({"type": "start"}))

            # Gameplay input (only if active and alive)
            if self.GAME_STATE == "ACTIVE" and our_player_alive:
                if event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
                    self._process_local_shot(event.pos, action_queue)

                # Key tracking for continuous movement
                if event.type == pygame.KEYDOWN:
                    key_name = pygame.key.name(event.key)
                    if key_name in self.keys_down: self.keys_down[key_name] = True
                elif event.type == pygame.KEYUP:
                    key_name = pygame.key.name(event.key)
                    if key_name in self.keys_down: self.keys_down[key_name] = False

        # Generate movement actions based on currently held keys.
        if self.GAME_STATE == "ACTIVE" and our_player_alive:
            self._process_continuous_movement(action_queue)

        return running

    def _process_local_shot(self, pos, action_queue: asyncio.Queue):
        """Checks the local cooldown before sending a shot action."""
        current_time = time.time()
        # CLIENT-SIDE PREDICTION: Check cooldown locally to avoid sending invalid messages.
        if (current_time - self.local_last_shot_time) > SHOOT_COOLDOWN:
            self.local_last_shot_time = current_time
            asyncio.create_task(action_queue.put({"type": "shoot", "target": pos}))

    def _process_continuous_movement(self, action_queue: asyncio.Queue):
        """Sends movement actions for keys currently held down."""
        for key, pressed in self.keys_down.items():
            if pressed:
                asyncio.create_task(action_queue.put({"type": "move", "dir": key}))

    # ---- Rendering ---------------------------------------------------------
    def render(self):
        """Renders the current game state to the screen."""
        self.screen.fill((25, 25, 35)) # Background color

        if self.GAME_STATE == "WAITING":
            self._render_lobby()
        elif self.GAME_STATE == "ACTIVE" or self.GAME_STATE == "GAME_OVER":
            self._render_gameplay()
            if self.GAME_STATE == "GAME_OVER":
                self._render_game_over()

        pygame.display.flip() # Update the display

    def _render_lobby(self):
        """Renders the waiting screen."""
        # Draw players
        for data in self.players.values():
            pygame.draw.rect(self.screen, data["color"], pygame.Rect(data["x"], data["y"], PLAYER_SIZE, PLAYER_SIZE))

        count_text = self.font.render(f"{len(self.players)} Players Connected", True, (255, 255, 255))
        start_text = self.font.render("Press SPACE to Start", True, (200, 200, 200))
        self.screen.blit(count_text, (SCREEN_WIDTH/2 - count_text.get_width()/2, 40))
        self.screen.blit(start_text, (SCREEN_WIDTH/2 - start_text.get_width()/2, 90))

    def _render_gameplay(self):
        """Renders the active game elements (players, health, projectiles)."""
        # Draw living players and their health bars.
        for p_data in self.players.values():
            if p_data['health'] > 0:
                # Draw health bar background (red)
                pygame.draw.rect(self.screen, (100, 0, 0), pygame.Rect(p_data['x'], p_data['y'] - 15, PLAYER_SIZE, 10))
                # Draw current health bar (green)
                health_width = (p_data['health'] / MAX_HEALTH) * PLAYER_SIZE
                pygame.draw.rect(self.screen, (0, 200, 0), pygame.Rect(p_data['x'], p_data['y'] - 15, health_width, 10))
                # Draw player square
                pygame.draw.rect(self.screen, p_data["color"], pygame.Rect(p_data['x'], p_data['y'], PLAYER_SIZE, PLAYER_SIZE))

        # Draw all projectiles.
        for proj in self.projectiles:
            pygame.draw.circle(self.screen, (255, 255, 100), (proj['x'], proj['y']), PROJECTILE_SIZE)

    def _render_game_over(self):
        """Renders the game over screen and the winner."""
        over_text = self.font.render("GAME OVER", True, (255, 0, 0))
        self.screen.blit(over_text, (SCREEN_WIDTH/2 - over_text.get_width()/2, SCREEN_HEIGHT/2 - 50))
        if self.winner_id and self.winner_id in self.players:
            # Display a snippet of the winner's ID for readability
            winner_snippet = self.winner_id.splitlines()[-2][-10:]
            winner_text = self.font.render(f"Winner: {winner_snippet}", True, self.players[self.winner_id]['color'])
            self.screen.blit(winner_text, (SCREEN_WIDTH/2 - winner_text.get_width()/2, SCREEN_HEIGHT/2 + 10))