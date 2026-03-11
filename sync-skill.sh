#!/bin/bash

# Sync Cobo Agentic Wallet skill from source repository
# Usage: ./sync-skill.sh

set -e

# Define source and target paths
SOURCE_DIR="../cobo-agent-wallets/cobo-agent-wallet/sdk/skills/cobo-agentic-wallet"
TARGET_DIR="./skills/cobo-agentic-wallet"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Starting skill synchronization...${NC}"

# Check if source directory exists
if [ ! -d "$SOURCE_DIR" ]; then
    echo -e "${RED}Error: Source directory does not exist: $SOURCE_DIR${NC}"
    exit 1
fi

# Create target directory if it doesn't exist
if [ ! -d "$TARGET_DIR" ]; then
    echo -e "${YELLOW}Creating target directory: $TARGET_DIR${NC}"
    mkdir -p "$TARGET_DIR"
fi

# Sync files using rsync
echo -e "${YELLOW}Syncing files from $SOURCE_DIR to $TARGET_DIR${NC}"
rsync -av --delete \
    --exclude='.git' \
    --exclude='.DS_Store' \
    --exclude='node_modules' \
    --exclude='evals' \
    "$SOURCE_DIR/" "$TARGET_DIR/"

echo -e "${GREEN}✓ Skill synchronization completed successfully!${NC}"

# Show what was synced
echo -e "\n${YELLOW}Synced files:${NC}"
ls -lh "$TARGET_DIR"