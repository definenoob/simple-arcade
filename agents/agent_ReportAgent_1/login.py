from crypto_utils import generate_key_pair
import os

# Create a directory for keys if it doesn't exist
os.makedirs("keys", exist_ok=True)

# Generate client keys
print("Generating client key pair...")
generate_key_pair(
    private_key_path="keys/client_private_key.pem",
    public_key_path="keys/client_public_key.pem"
)

print("\nKey generation complete.")
