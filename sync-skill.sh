#!/bin/bash

# Sync Cobo Agentic Wallet skill from source repository
# Usage: ./sync-skill.sh

set -e

# Define source and target paths
SKILLS_SOURCE_DIR="../cobo-agent-wallets/cobo-agent-wallet/sdk/skills"
GITHUB_URL="https://github.com/cobosteven/cobo-agent-wallet-manual/tree/master/skills/cobo-agentic-wallet"

# Skills to sync: source_subdir -> target_subdir
SKILL_MAPS=(
    "cobo-agentic-wallet-sandbox:cobo-agentic-wallet"
    "evm-defi-dex-swap:evm-defi-dex-swap"
    # "caw-eval:caw-eval"
)

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${YELLOW}Starting skill synchronization...${NC}"

# Check if source directory exists
if [ ! -d "$SKILLS_SOURCE_DIR" ]; then
    echo -e "${RED}Error: Source directory does not exist: $SKILLS_SOURCE_DIR${NC}"
    exit 1
fi

# Track synced target directories for post-sync replacements
SYNCED_DIRS=()

for MAP in "${SKILL_MAPS[@]}"; do
    SRC_NAME="${MAP%%:*}"
    TGT_NAME="${MAP##*:}"
    SRC_DIR="$SKILLS_SOURCE_DIR/$SRC_NAME"
    TGT_DIR="./skills/$TGT_NAME"

    if [ -d "$SRC_DIR" ]; then
        mkdir -p "$TGT_DIR"
        echo -e "${YELLOW}Syncing $SRC_NAME → $TGT_DIR${NC}"
        EXTRA_EXCLUDES=()
        [ "$SRC_NAME" = "caw-eval" ] && EXTRA_EXCLUDES+=(--exclude='README.md' --exclude='reports/')
        rsync -av --delete \
            --exclude='.git' \
            --exclude='.DS_Store' \
            --exclude='node_modules' \
            --exclude='evals' \
            "${EXTRA_EXCLUDES[@]}" \
            "$SRC_DIR/" "$TGT_DIR/"
        # Clean up renamed directories (e.g. recipes/ → references/)
        if [ -d "$TGT_DIR/recipes" ] && [ -d "$TGT_DIR/references" ]; then
            echo -e "${YELLOW}Removing stale $TGT_DIR/recipes/ (renamed to references/)${NC}"
            rm -rf "$TGT_DIR/recipes"
        fi
        SYNCED_DIRS+=("$TGT_DIR")
        echo -e "${GREEN}✓ Synced $SRC_NAME${NC}"
    else
        # Source removed — delete local target if it exists
        if [ -d "$TGT_DIR" ]; then
            echo -e "${RED}Source $SRC_DIR no longer exists, removing $TGT_DIR${NC}"
            rm -rf "$TGT_DIR"
        else
            echo -e "${YELLOW}Skipping $SRC_NAME (source not found)${NC}"
        fi
    fi
done

# Copy skills README.md from source
SKILLS_README="$SKILLS_SOURCE_DIR/README.md"
if [ -f "$SKILLS_README" ]; then
    cp "$SKILLS_README" "./skills/README.md"
    sed -i '' "s|/path/to/cobo-agentic-wallet/|${GITHUB_URL}|g" "./skills/README.md"
    echo -e "${GREEN}✓ Copied and updated skills README.md to ./skills/${NC}"
fi

# Post-sync replacements across all synced dirs + README
if [ ${#SYNCED_DIRS[@]} -gt 0 ] || [ -f "./skills/README.md" ]; then
    REPLACE_TARGETS=()
    for D in "${SYNCED_DIRS[@]}"; do
        [ -d "$D" ] && REPLACE_TARGETS+=("$D")
    done
    [ -f "./skills/README.md" ] && REPLACE_TARGETS+=("./skills/README.md")

    if [ ${#REPLACE_TARGETS[@]} -gt 0 ]; then
        echo -e "${YELLOW}Running post-sync replacements...${NC}"
        # Repo references
        find "${REPLACE_TARGETS[@]}" -type f -name '*.md' -exec \
            sed -i '' 's|CoboGlobal/cobo-agentic-wallet|cobosteven/cobo-agent-wallet-manual|g' {} +
        # Skill slug in install commands
        find "${REPLACE_TARGETS[@]}" -type f -name '*.md' -exec \
            sed -i '' 's|--skill cobo-agentic-wallet|--skill cobo-agentic-wallet-sandbox|g' {} +
        find "${REPLACE_TARGETS[@]}" -type f -name '*.md' -exec \
            sed -i '' 's|clawhub@latest install cobo-agentic-wallet|clawhub@latest install cobo-agentic-wallet-sandbox|g' {} +
        echo -e "${GREEN}✓ Repo references updated${NC}"
    fi
fi

echo -e "${GREEN}✓ Skill synchronization completed successfully!${NC}"

# Show what was synced
echo -e "\n${YELLOW}Synced directories:${NC}"
for D in "${SYNCED_DIRS[@]}"; do
    echo -e "  $D"
    ls -lh "$D"
    echo ""
done