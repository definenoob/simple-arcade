# `ReportAgent_1` - The Game Clock and Event Reporter

This agent serves a critical and specialized role: it is the **authoritative clock and event aggregator** for the multiplayer game. It does not contain any game logic itself. Instead, it listens for actions from all game clients, verifies their authenticity, bundles them into timed reports, and broadcasts these reports back to all players.

This architecture ensures that every player's game is synchronized to the same clock and the same sequence of events, which is essential for fair and consistent multiplayer gameplay.

---

## Behavior

The `ReportAgent` follows a strict, continuous loop to ensure a steady flow of game state information.

<details>
<summary><b>(Click to expand)</b> The agent's operational loop:</summary>
<br>

1.  **Listens for Player Actions**: The agent connects to the server and waits for messages from any of the `GameAgent` clients (e.g., `player.join`, `player.move`, `player.shoot`).

2.  **Verifies Every Message**: A `RECEIVE` hook (`verify_incoming_message`) intercepts every message.
    * It expects messages to be in a `SignedWrapper` format.
    * It uses the public key included in the wrapper to verify the digital signature. This proves the message came from a legitimate player and was not tampered with.
    * If a message is valid, the hook passes the **entire, original signed message** forward. Invalid or unsigned messages are silently discarded.

3.  **Buffers Validated Actions**: The main receive handler (`custom_receive`) takes the verified messages from the hook and places them into an internal `asyncio.Queue` called `message_buffer`.

4.  **Creates Timed Batch Reports**: The send handler (`custom_send`) runs on a loop controlled by `FPS` (Frames Per Second). In each "frame," it:
    * Drains all messages currently in the `message_buffer`.
    * Measures the **exact time elapsed** in nanoseconds since the last report was sent. This becomes the `deltaTiming` value.
    * Constructs a `batch.report` payload, placing the list of original, signed player actions into the `deltaEvents` field.

5.  **Signs and Broadcasts the Report**: Before the `batch.report` is sent, a `SEND` hook (`sign_outgoing_message`) signs the entire report with the `ReportAgent_1`'s own private key. This authenticates the report itself. The server then broadcasts this signed report to all connected game clients.

The game clients receive this report, verify its signature, and then use the `deltaTiming` and `deltaEvents` within it to advance their local simulation of the game. 

</details>

---

## SDK Features Used

| Feature                         | Description                                                                                               |
| ------------------------------- | --------------------------------------------------------------------------------------------------------- |
| `SummonerClient(name=...)`      | Instantiates and manages the agent's connection.                                                          |
| `@client.hook(direction=...)`   | Intercepts all incoming and outgoing messages to enforce cryptographic signing and verification.          |
| `@client.receive(route="")`     | Buffers the verified, signed messages from players into an internal queue.                                |
| `@client.send(route="")`        | Periodically drains the buffer, measures the time delta, and constructs the authoritative `batch.report`. |
| `client.run(host, port, ...)`   | Connects to the server and starts the main networking event loop.                                         |

---

## How to Run

The `ReportAgent` is a crucial part of the multiplayer setup and must be running for the game to function.

**1. Start the Server**
This is the central message hub.
```bash
# In Terminal 1
python test_server.py
````

**2. Start the Report Agent**
This agent will connect to the server and begin its reporting loop.

```bash
# In Terminal 2
python agents/agent_ReportAgent_1/agent.py
```

**3. Start the Game Clients**
With the server and reporter running, players can now join.

```bash
# In Terminal 3 (Player 1)
python agents/agent_ChatAgent_0/agent.py --name alice

# In Terminal 4 (Player 2)
python agents/agent_ChatAgent_0/agent.py --name bob
```

-----

## Why This Architecture is So Effective

The "magic" behind this setup isn't magic at all; it's a clever and robust separation of concerns, built on a foundation of cryptographic trust.

### 1. The `ReportAgent` is a "Dumb Clock," and That's a Good Thing 🧠

The `ReportAgent` is the heart of the synchronization, but it has zero knowledge of the game's rules. It doesn't know what a "player" is, what "health" means, or how fast a projectile should move.

  * **Its Only Job:** Collect signed messages, bundle them into a list (`deltaEvents`), measure the precise time since the last bundle (`deltaTiming`), and broadcast the result.

This is incredibly powerful because it decouples the game logic from the network synchronization logic. The `ReportAgent` is just an **authoritative, metronomic event bus**. This design ensures that every `PlayerAgent` receives the exact same list of events in the exact same order and with the exact same timing information.

### 2. The `PlayerAgent` is a Deterministic Simulation ⚙️

Each `PlayerAgent` runs a complete, self-contained game engine. This engine is **deterministic**, meaning that if you give it the same starting conditions and the same sequence of inputs, it will *always* produce the exact same outcome.

When a `PlayerAgent` receives a batch report, it doesn't just update its state. It feeds the `deltaEvents` (the inputs) and `deltaTiming` (the clock tick) into its simulation. Since every client gets the same report and runs the same simulation code, they all independently arrive at the identical game state. This is why when Alice shoots on her screen, Bob sees the shot happen in perfect sync on his screen.

A great example of this in your code is how player colors and spawn points are generated: `random.seed(player_id)`. This ensures every client generates the same color and starting position for "alice" without needing the server to send that data explicitly.

### 3. Cryptography Creates a "Web of Trust" 🔐

In a normal client-server game, the server is the single source of truth. Here, there is no central authority. Trust is established peer-to-peer using digital signatures:

  * **Authentication:** When a `PlayerAgent` receives an action inside a batch report, it can independently verify the signature on that action. It knows with cryptographic certainty that the action came from the player who owns the public key. It's impossible to impersonate another player.
  * **Integrity:** The signature is based on the exact content of the message (e.g., `"type": "move", "dir": "w"`). If an attacker intercepted the message and changed the direction to `"d"`, the signature would no longer be valid. The `PlayerAgent`'s `receiver_handler` would detect this tampering and discard the event. This prevents cheating.

This system allows every player to trust the actions of every other player, creating a secure environment without a central referee.