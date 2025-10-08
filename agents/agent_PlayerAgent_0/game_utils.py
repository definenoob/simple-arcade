# game_utils.py
# Encapsulates the game engine, including state management, logic, rendering, and input.

import pygame
import random
import time
import uuid
import asyncio
import math
from math import sqrt
from typing import Dict, Any, List, Tuple, Optional
from pydantic import ValidationError
from abc import ABC, abstractmethod

# Import the data models
try:
    from models import JsonRpcRequest, SignedWrapper, BatchReportParams
except ImportError:
    print("Error: models.py not found.")
    exit(1)

# ---- Game Interface Definition ---------------------------------------------

class IGameEngine(ABC):
    """
    Defines the public interface for a game engine, ensuring that any
    concrete implementation provides the essential methods for managing
    the game's lifecycle.
    """

    @abstractmethod
    def process_report(self, report_params: BatchReportParams, action_queue: asyncio.Queue) -> None:
        """Processes an authoritative batch report to update the game state."""
        pass

    @abstractmethod
    def handle_input(self, action_queue: asyncio.Queue) -> bool:
        """Handles user input and returns a flag to continue or quit."""
        pass

    @abstractmethod
    def render(self) -> None:
        """Renders the current game state to the display."""
        pass

    @abstractmethod
    def shutdown(self) -> None:
        """Cleanly shuts down the game engine and its resources."""
        pass

# ---- Game Constants --------------------------------------------------------
SCREEN_WIDTH = 800
SCREEN_HEIGHT = 600
PLAYER_SIZE = 40
# Speeds are defined per second for frame-rate independence.
PLAYER_SPEED_PER_SECOND = 4500
PROJECTILE_SPEED_PER_SECOND = 1000
PROJECTILE_SIZE = 5
# Cooldown period for shooting (official game rule).
SHOOT_COOLDOWN = 0.20
MAX_HEALTH = 10

# ---- Camera Class ----------------------------------------------------------
class Camera:
    """Manages the viewport for an infinite playing field."""
    
    def __init__(self, screen_width: int, screen_height: int, zoom: float = 2.5):
        self.x = 0.0
        self.y = 0.0
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.zoom = zoom  # Zoom level (higher = more zoomed out)
    
    def update(self, target_x: float, target_y: float):
        """Center the camera on the target position (with smooth following)."""
        # Center the camera on the target
        self.x = target_x - (self.screen_width * self.zoom) / 2
        self.y = target_y - (self.screen_height * self.zoom) / 2
    
    def world_to_screen(self, world_x: float, world_y: float) -> Tuple[int, int]:
        """Convert world coordinates to screen coordinates."""
        screen_x = int((world_x - self.x) / self.zoom)
        screen_y = int((world_y - self.y) / self.zoom)
        return screen_x, screen_y
    
    def screen_to_world(self, screen_x: int, screen_y: int) -> Tuple[float, float]:
        """Convert screen coordinates to world coordinates."""
        world_x = screen_x * self.zoom + self.x
        world_y = screen_y * self.zoom + self.y
        return world_x, world_y
    
    def is_visible(self, world_x: float, world_y: float, margin: int = 100) -> bool:
        """Check if a world position is visible on screen (with margin for culling)."""
        screen_x, screen_y = self.world_to_screen(world_x, world_y)
        return (-margin <= screen_x <= self.screen_width + margin and 
                -margin <= screen_y <= self.screen_height + margin)

# ---- GameEngine Class ------------------------------------------------------
class GameEngine(IGameEngine):
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
        
        # Initialize Camera
        self.camera = Camera(SCREEN_WIDTH, SCREEN_HEIGHT)

    def shutdown(self) -> None:
        """Cleanly shuts down the Pygame instance."""
        if pygame.get_init():
            pygame.quit()
            print("Pygame shut down cleanly.")

    # ---- Core Update Logic (The Game Tick) ---------------------------------
    def process_report(self, report_params: BatchReportParams, action_queue: asyncio.Queue) -> None:
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
        
        # --- Tick Part 4: Update Camera Position ---
        self._update_camera()

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

            # Note: We don't remove projectiles for going off-screen anymore (infinite field)
            # But we could add a max distance/time limit if desired for performance

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
            "x": random.randint(-1000, 1000),  # Spawn in a wider area
            "y": random.randint(-1000, 1000),
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
        # No boundary constraints - infinite field!

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
    
    def _update_camera(self):
        """Update camera to follow the local player."""
        if self.client_public_key in self.players:
            player = self.players[self.client_public_key]
            # Center camera on local player
            target_x = player['x'] + PLAYER_SIZE / 2
            target_y = player['y'] + PLAYER_SIZE / 2
            self.camera.update(target_x, target_y)

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
            # Convert screen coordinates to world coordinates for shooting
            world_x, world_y = self.camera.screen_to_world(pos[0], pos[1])
            # Convert to integers for Pydantic validation
            asyncio.create_task(action_queue.put({"type": "shoot", "target": (int(world_x), int(world_y))}))

    def _process_continuous_movement(self, action_queue: asyncio.Queue):
        """Sends movement actions for keys currently held down."""
        for key, pressed in self.keys_down.items():
            if pressed:
                asyncio.create_task(action_queue.put({"type": "move", "dir": key}))

    # ---- Rendering ---------------------------------------------------------
    def render(self) -> None:
        """Renders the current game state to the screen."""
        self.screen.fill((25, 25, 35)) # Background color
        
        # Draw a subtle grid to show movement
        self._draw_grid()

        if self.GAME_STATE == "WAITING":
            self._render_lobby()
        elif self.GAME_STATE == "ACTIVE" or self.GAME_STATE == "GAME_OVER":
            self._render_gameplay()
            if self.GAME_STATE == "GAME_OVER":
                self._render_game_over()

        pygame.display.flip() # Update the display
    
    def _draw_grid(self):
        """Draw a background grid to visualize movement in infinite space."""
        grid_size = 100 * self.camera.zoom  # Scale grid with zoom
        grid_color = (40, 40, 50)
        
        # Calculate grid offset based on camera position
        offset_x = int((self.camera.x / self.camera.zoom) % grid_size)
        offset_y = int((self.camera.y / self.camera.zoom) % grid_size)
        
        # Draw vertical lines
        for x in range(-offset_x, SCREEN_WIDTH + int(grid_size), int(grid_size)):
            pygame.draw.line(self.screen, grid_color, (x, 0), (x, SCREEN_HEIGHT))
        
        # Draw horizontal lines
        for y in range(-offset_y, SCREEN_HEIGHT + int(grid_size), int(grid_size)):
            pygame.draw.line(self.screen, grid_color, (0, y), (SCREEN_WIDTH, y))

    def _render_lobby(self):
        """Renders the waiting screen."""
        # Draw players (with camera transformation)
        for data in self.players.values():
            screen_x, screen_y = self.camera.world_to_screen(data["x"], data["y"])
            if self.camera.is_visible(data["x"], data["y"]):
                pygame.draw.rect(self.screen, data["color"], 
                               pygame.Rect(screen_x, screen_y, PLAYER_SIZE / self.camera.zoom, PLAYER_SIZE / self.camera.zoom))

        count_text = self.font.render(f"{len(self.players)} Players Connected", True, (255, 255, 255))
        start_text = self.font.render("Press SPACE to Start", True, (200, 200, 200))
        self.screen.blit(count_text, (SCREEN_WIDTH/2 - count_text.get_width()/2, 40))
        self.screen.blit(start_text, (SCREEN_WIDTH/2 - start_text.get_width()/2, 90))
        
        # Draw edge indicators
        self._draw_edge_indicators()

    def _render_gameplay(self):
        """Renders the active game elements (players, health, projectiles)."""
        # Calculate scaled sizes
        scaled_player_size = PLAYER_SIZE / self.camera.zoom
        scaled_health_bar_height = 10 / self.camera.zoom
        scaled_health_bar_offset = 15 / self.camera.zoom
        
        # Draw living players and their health bars (with camera transformation)
        for p_data in self.players.values():
            if p_data['health'] > 0 and self.camera.is_visible(p_data['x'], p_data['y']):
                screen_x, screen_y = self.camera.world_to_screen(p_data['x'], p_data['y'])
                
                # Draw health bar background (red)
                pygame.draw.rect(self.screen, (100, 0, 0), 
                               pygame.Rect(screen_x, screen_y - scaled_health_bar_offset, 
                                         scaled_player_size, scaled_health_bar_height))
                # Draw current health bar (green)
                health_width = (p_data['health'] / MAX_HEALTH) * scaled_player_size
                pygame.draw.rect(self.screen, (0, 200, 0), 
                               pygame.Rect(screen_x, screen_y - scaled_health_bar_offset, 
                                         health_width, scaled_health_bar_height))
                # Draw player square
                pygame.draw.rect(self.screen, p_data["color"], 
                               pygame.Rect(screen_x, screen_y, scaled_player_size, scaled_player_size))

        # Draw all projectiles (with camera transformation and culling)
        scaled_projectile_size = max(2, PROJECTILE_SIZE / self.camera.zoom)
        for proj in self.projectiles:
            if self.camera.is_visible(proj['x'], proj['y']):
                screen_x, screen_y = self.camera.world_to_screen(proj['x'], proj['y'])
                pygame.draw.circle(self.screen, (255, 255, 100), (int(screen_x), int(screen_y)), int(scaled_projectile_size))
        
        # Draw UI elements
        self._draw_position_indicator()
        self._draw_edge_indicators()

    def _draw_position_indicator(self):
        """Draw player position coordinates in the corner."""
        if self.client_public_key in self.players:
            player = self.players[self.client_public_key]
            pos_text = self.small_font.render(
                f"Position: ({int(player['x'])}, {int(player['y'])})", 
                True, (200, 200, 200)
            )
            self.screen.blit(pos_text, (10, 10))
    
    def _draw_edge_indicators(self):
        """Draw triangular indicators at screen edges pointing toward off-screen players."""
        if self.client_public_key not in self.players:
            return
        
        local_player = self.players[self.client_public_key]
        local_x = local_player['x'] + PLAYER_SIZE / 2
        local_y = local_player['y'] + PLAYER_SIZE / 2
        
        # Check each player
        for player_id, player_data in self.players.items():
            # Skip self and dead players
            if player_id == self.client_public_key or player_data['health'] <= 0:
                continue
            
            # Get player center position
            player_x = player_data['x'] + PLAYER_SIZE / 2
            player_y = player_data['y'] + PLAYER_SIZE / 2
            
            # Check if player is visible on screen
            if self.camera.is_visible(player_x, player_y, margin=-50):
                continue  # Player is on screen, skip indicator
            
            # Calculate direction from local player to this player
            dx = player_x - local_x
            dy = player_y - local_y
            distance = sqrt(dx * dx + dy * dy)
            
            if distance < 1:
                continue  # Too close, skip
            
            # Normalize direction
            dx /= distance
            dy /= distance
            
            # Convert local player position to screen center
            screen_center_x = SCREEN_WIDTH / 2
            screen_center_y = SCREEN_HEIGHT / 2
            
            # Calculate where the line to the enemy intersects the screen edge
            # We'll check all four edges and find the intersection
            edge_x, edge_y = self._calculate_edge_intersection(
                screen_center_x, screen_center_y, dx, dy
            )
            
            # Draw the triangle indicator
            self._draw_triangle_indicator(edge_x, edge_y, dx, dy, player_data['color'])
    
    def _calculate_edge_intersection(self, center_x: float, center_y: float, 
                                     dir_x: float, dir_y: float) -> Tuple[float, float]:
        """Calculate where a ray from center intersects the screen edge."""
        margin = 30  # Distance from edge
        
        # Calculate intersection with each edge
        intersections = []
        
        # Left edge (x = margin)
        if dir_x < 0:
            t = (margin - center_x) / dir_x
            y = center_y + t * dir_y
            if margin <= y <= SCREEN_HEIGHT - margin:
                intersections.append((margin, y))
        
        # Right edge (x = SCREEN_WIDTH - margin)
        if dir_x > 0:
            t = (SCREEN_WIDTH - margin - center_x) / dir_x
            y = center_y + t * dir_y
            if margin <= y <= SCREEN_HEIGHT - margin:
                intersections.append((SCREEN_WIDTH - margin, y))
        
        # Top edge (y = margin)
        if dir_y < 0:
            t = (margin - center_y) / dir_y
            x = center_x + t * dir_x
            if margin <= x <= SCREEN_WIDTH - margin:
                intersections.append((x, margin))
        
        # Bottom edge (y = SCREEN_HEIGHT - margin)
        if dir_y > 0:
            t = (SCREEN_HEIGHT - margin - center_y) / dir_y
            x = center_x + t * dir_x
            if margin <= x <= SCREEN_WIDTH - margin:
                intersections.append((x, SCREEN_HEIGHT - margin))
        
        # Return the closest intersection (should only be one valid)
        if intersections:
            return intersections[0]
        
        # Fallback (shouldn't happen)
        return center_x, center_y
    
    def _draw_triangle_indicator(self, x: float, y: float, dir_x: float, dir_y: float, color: Tuple[int, int, int]):
        """Draw a triangle pointing in the given direction."""
        # Triangle size
        size = 15
        
        # Calculate angle from direction
        angle = math.atan2(dir_y, dir_x)
        
        # Calculate triangle points (pointing toward the target)
        # Tip of the triangle
        tip_x = x + math.cos(angle) * size
        tip_y = y + math.sin(angle) * size
        
        # Base corners (perpendicular to direction)
        perp_angle1 = angle + math.pi * 2.5 / 3
        perp_angle2 = angle - math.pi * 2.5 / 3
        
        base1_x = x + math.cos(perp_angle1) * size * 0.6
        base1_y = y + math.sin(perp_angle1) * size * 0.6
        
        base2_x = x + math.cos(perp_angle2) * size * 0.6
        base2_y = y + math.sin(perp_angle2) * size * 0.6
        
        # Draw filled triangle
        points = [(tip_x, tip_y), (base1_x, base1_y), (base2_x, base2_y)]
        pygame.draw.polygon(self.screen, color, points)
        
        # Draw outline for visibility
        pygame.draw.polygon(self.screen, (255, 255, 255), points, 2)

    def _render_game_over(self):
        """Renders the game over screen and the winner."""
        over_text = self.font.render("GAME OVER", True, (255, 0, 0))
        self.screen.blit(over_text, (SCREEN_WIDTH/2 - over_text.get_width()/2, SCREEN_HEIGHT/2 - 50))
        if self.winner_id and self.winner_id in self.players:
            # Display a snippet of the winner's ID for readability
            winner_snippet = self.winner_id.splitlines()[-2][-10:]
            winner_text = self.font.render(f"Winner: {winner_snippet}", True, self.players[self.winner_id]['color'])
            self.screen.blit(winner_text, (SCREEN_WIDTH/2 - winner_text.get_width()/2, SCREEN_HEIGHT/2 + 10))