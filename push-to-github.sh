#!/usr/bin/env bash
# Connects this folder to your GitHub repo and pushes it.
# Safe to re-run: it fixes the remote instead of erroring if one already exists.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

bold=$(tput bold 2>/dev/null || true); dim=$(tput dim 2>/dev/null || true)
red=$(tput setaf 1 2>/dev/null || true); grn=$(tput setaf 2 2>/dev/null || true)
ylw=$(tput setaf 3 2>/dev/null || true); rst=$(tput sgr0 2>/dev/null || true)

echo
echo "${bold}Push 'Job Notis' to GitHub${rst}"
echo "${dim}Find your username at github.com — it's in the top-right avatar menu.${rst}"
echo

read -r -p "GitHub username: " USER
[ -z "$USER" ] && { echo "${red}No username entered. Run this again.${rst}"; exit 1; }

read -r -p "Repository name [job-notis]: " REPO
REPO="${REPO:-job-notis}"

URL="https://github.com/${USER}/${REPO}.git"
echo
echo "Connecting to ${bold}${URL}${rst}"

git branch -M main
if git remote get-url origin >/dev/null 2>&1; then
  git remote set-url origin "$URL"
else
  git remote add origin "$URL"
fi

# Store credentials in the macOS keychain so the token is entered only once.
git config credential.helper osxkeychain 2>/dev/null || true

cat <<EOF

${ylw}${bold}When it asks you to log in:${rst}
  Username: ${bold}${USER}${rst}
  Password: ${bold}paste your Personal Access Token${rst} ${dim}(NOT your GitHub password —
            GitHub stopped accepting those. Instructions are in the chat.)${rst}

${dim}The token stays hidden as you paste it. That's normal — just press Enter.${rst}

EOF

echo "Pushing..."
if git push -u origin main; then
  cat <<EOF

${grn}${bold}Pushed successfully.${rst}

Your code is now at:  ${bold}https://github.com/${USER}/${REPO}${rst}

Next, add your secrets here:
  ${bold}https://github.com/${USER}/${REPO}/settings/secrets/actions${rst}

Then enable and run the workflow:
  ${bold}https://github.com/${USER}/${REPO}/actions${rst}
EOF
else
  cat <<EOF

${red}${bold}Push failed.${rst} The usual causes:

  ${bold}"Repository not found"${rst}
      The repo name or username is wrong, or the repo was never created.
      Check that ${URL%.git} opens in your browser.

  ${bold}"refusing to allow a Personal Access Token to create or update workflow"${rst}
      Your token is missing the ${bold}workflow${rst} scope. Create a new token
      with both ${bold}repo${rst} and ${bold}workflow${rst} ticked, then run this script again.

  ${bold}"Authentication failed"${rst}
      You entered your GitHub password instead of a Personal Access Token.

Copy the error text into the chat and I'll sort it out.
EOF
  exit 1
fi
