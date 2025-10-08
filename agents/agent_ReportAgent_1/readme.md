# `ReportAgent_1`

A secure, buffered reporting agent. It validates the digital signature of all incoming messages, aggregates them into batches based on a fixed time interval (frame rate), and sends a single, signed report containing the collection of original, validated messages.

This agent demonstrates a security-conscious pipeline where it validates upstream messages before including them in its own signed reports, preserving the original signatures for downstream consumers.

## Behavior

<details>
<summary><b>(Click to expand)</b> The agent goes through these steps:</summary>
<br>

1.  **Verification Hook:** The `verify_incoming_message` hook (`@client.hook(direction=Direction.RECEIVE)`) intercepts all incoming messages.
    * It parses the message expecting a `SignedWrapper` format (`payload`, `signature`, `public_key`).
    * It uses the provided public key to verify the signature against the payload.
    * If the signature is valid, it returns the **entire, original signed message** to be processed further. Invalid or unsigned messages are silently discarded.
2.  **Receive Handler:** The receive handler (`@client.receive(route="")`) receives the validated, signed message object from the hook.
    * It enqueues the full object into an internal `asyncio.Queue` called `message_buffer`.
3.  **Send Handler:** The send handler (`@client.send(route="")`) runs in a loop controlled by `FPS` (Frames Per Second).
    * In each "frame," it drains all buffered messages from the queue.
    * It constructs a JSON-RPC `batch.report` payload, where the `deltaEvents` field is a list of the original signed messages it collected.
4.  **Signing Hook:** Before the batch report is sent, the `sign_outgoing_message` hook (`@client.hook(direction=Direction.SEND)`) signs the entire report using the `ReportAgent_1`'s own private key.

This creates a nested signature structure: the reporter's signature provides authenticity for the *batch*, while the preserved signatures within the batch provide authenticity for each individual *event*.

</details>

## SDK Features Used

| Feature                         | Description                                                              |
| ------------------------------- | ------------------------------------------------------------------------ |
| `SummonerClient(name=...)`      | Instantiates and manages the agent                                       |
| `@client.hook(direction=...)`   | Intercepts messages to add security (signing and verification)           |
| `@client.receive(route="")`     | Buffers the verified, signed messages into an internal queue             |
| `@client.send(route="")`        | Periodically drains the buffer and constructs the signed batch report    |
| `client.run(host, port, ...)`   | Connects to the server and starts the asyncio event loop                 |

## How to Run

First, start the Summoner server:

```bash
python test_server.py