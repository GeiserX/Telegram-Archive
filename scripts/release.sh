#!/bin/bash
# Release script - ensures CHANGELOG is updated before tagging
#
# Usage: ./scripts/release.sh v4.1.5
#
# This script:
# 1. Validates the version format
# 2. Checks that CHANGELOG.md has an entry for this version
# 3. Creates and pushes the git tag
# 4. The GitHub Action will then create the release with changelog notes

set -e

VERSION="$1"

if [ -z "$VERSION" ]; then
    echo "❌ Usage: $0 <version>"
    echo "   Example: $0 v4.1.5"
    exit 1
fi

# Validate version format
if [[ ! "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "❌ Invalid version format: $VERSION"
    echo "   Expected format: vX.Y.Z (e.g., v4.1.5)"
    exit 1
fi

# Extract version without 'v' prefix for changelog lookup
VERSION_NUM="${VERSION#v}"

# Check if CHANGELOG has entry for this version
if ! grep -q "## \[$VERSION_NUM\]" docs/CHANGELOG.md; then
    echo "❌ No changelog entry found for version $VERSION_NUM"
    echo ""
    echo "Please add an entry to docs/CHANGELOG.md:"
    echo ""
    echo "## [$VERSION_NUM] - $(date +%Y-%m-%d)"
    echo ""
    echo "### Added/Changed/Fixed"
    echo "- Your changes here"
    echo ""
    exit 1
fi

echo "✅ Changelog entry found for $VERSION_NUM"

# Check for uncommitted changes
if ! git diff --quiet HEAD; then
    echo "❌ You have uncommitted changes. Please commit first."
    exit 1
fi

# Check we're on master/main
BRANCH=$(git branch --show-current)
if [[ "$BRANCH" != "master" && "$BRANCH" != "main" ]]; then
    echo "⚠️  Warning: You're on branch '$BRANCH', not master/main"
    read -p "Continue anyway? (y/N) " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Create and push tag
echo "📦 Creating tag $VERSION..."
git tag "$VERSION" -m "Release $VERSION"

echo "🚀 Pushing tag to origin..."
git push origin "$VERSION"

echo ""
echo "✅ Release $VERSION initiated!"
echo ""
echo "GitHub Actions will now:"
echo "  1. Build and push Docker images"
echo "  2. Create GitHub release with changelog notes"
echo ""
echo "Monitor at: https://github.com/GeiserX/Telegram-Archive/actions"
