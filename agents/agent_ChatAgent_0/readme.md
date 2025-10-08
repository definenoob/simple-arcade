# Multiplayer Game Agent

This agent is a fully-featured, secure, peer-to-peer multiplayer game client built with Pygame. It demonstrates a complete gameplay loop, including a lobby, active gameplay, and a game-over state, all synchronized through a `ReportAgent`.

---

## Gameplay Features

* **Lobby System**: When clients connect, they enter a waiting lobby where they can see all other connected players. The game starts when any player presses the **SPACEBAR**.
* **Live Gameplay**: Players control a colored square with the **WASD** keys and fire projectiles with a **mouse click**.
* **Health and Combat**: Each player has **10 health**. Projectiles deal **1 damage**. A player is eliminated when their health reaches zero.
* **Win Condition**: The last player with health remaining wins the game, at which point a "Game Over" screen is displayed declaring the winner.

---

## Technical Features

* **Peer-to-Peer Synchronization**: The game state is synchronized without a central game server. Each client processes a stream of events from a `ReportAgent` to build an identical simulation.
* **Cryptographic Identity**: Each player has a unique identity secured by a public/private key pair. All actions are digitally signed to prove their origin.
* **Authoritative Clock**: Player and projectile movement is timed by the `deltaTiming` value provided by the `ReportAgent`, ensuring smooth, consistent physics regardless of client performance or network lag.
* **Anti-Cheat**: The game features peer-side enforcement for game rules. For example, if a modified client attempts to fire faster than the allowed cooldown, their invalid shots are ignored by all other players.

---

## How to Set Up a Multiplayer Game

Setting up a multiplayer match involves running three components: the server, the reporter agent, and at least two game agents (one for each player).

### Step 1: Generate Player Identities

Before playing, you need to create a unique cryptographic identity for each player. Use the included `login.py` script with a `--name` argument.

Open a terminal and run the following commands:

```bash
# Create an identity for player "alice"
python agents/agent_ChatAgent_0/login.py --name alice

# Create an identity for player "bob"
python agents/agent_ChatAgent_0/login.py --name bob
```

### Step 2: Run the Server

```bash
# In a new terminal (Terminal 1)
# Remember to tweak the `rate_limit_msgs_per_minute` of `test_server_config.json` to 36000
python test_server.py
```

### Step 3: Start the Reporter

```bash
# In a new terminal (Terminal 2)
python agents/agent_ReportAgent_1/agent.py
```

### Step 4: Start the Game Clients

```bash
# In a new terminal (Terminal 3), start a client for "alice"
python agents/agent_ChatAgent_0/agent.py --name alice

# In a new terminal (Terminal 4), start a client for "bob"
python agents/agent_ChatAgent_0/agent.py --name bob
```

Two Pygame windows will appear. You will see both players in the lobby in each window.

### Step 5: Play the Game!

In either game window, press the SPACEBAR to start the match. The game will begin for all players.

Use WASD to move your player.

Click the mouse to fire projectiles at your opponent.

The last player with health remaining wins!