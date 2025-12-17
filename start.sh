#!/usr/bin/env bash
# start.sh — uniwersalny skrypt startowy dla aplikacji Node (monorepo-safe)
# Komentarze i komunikaty po polsku (Twoje preferencje).

set -euo pipefail

# Przechodzimy do katalogu skryptu
BASE_DIR="$(cd "$(dirname "$0")" && pwd)"

# Wybieramy katalog aplikacji:
if [ -f "$BASE_DIR/package.json" ]; then
  APP_DIR="$BASE_DIR"
elif [ -f "$BASE_DIR/node-backend/package.json" ]; then
  APP_DIR="$BASE_DIR/node-backend"
else
  echo "❌ Nie znaleziono package.json w katalogu głównym ani w ./node-backend"
  echo "Upewnij się, że package.json znajduje się w jednym z tych miejsc."
  exit 1
fi

cd "$APP_DIR"
echo "📁 Working directory: $APP_DIR"

# Instalacja zależności: preferujemy yarn/pnpm jeśli są lockfile i dostępne narzędzie
if [ -f yarn.lock ] && command -v yarn >/dev/null 2>&1; then
  echo "📦 Instaluję zależności przez yarn..."
  yarn install --frozen-lockfile
elif [ -f pnpm-lock.yaml ] && command -v pnpm >/dev/null 2>&1; then
  echo "📦 Instaluję zależności przez pnpm..."
  pnpm install --frozen-lockfile
else
  echo "📦 Instaluję zależności przez npm..."
  # npm ci szybciej i deterministycznie, fallback na npm install
  npm ci || npm install
fi

# Jeśli jest skrypt build w package.json — uruchamiamy
if grep -q '"build"' package.json 2>/dev/null; then
  echo "🔨 Znaleziono skrypt 'build' — uruchamiam: npm run build"
  npm run build
fi

# PORT: Railway dostarczy $PORT — jeśli nie ma, ustawiamy domyślny 3000
: "${PORT:=3000}"
export PORT
echo "🚀 Uruchamiam aplikację (PORT=$PORT)"

# Preferuj npm start jeśli istnieje, w przeciwnym wypadku spróbuj uruchomić typowy plik wejściowy
if grep -q '"start"' package.json 2>/dev/null; then
  echo "➡️ Wykryto 'start' w package.json — exec npm start"
  exec npm start
else
  for f in index.js server.js src/index.js src/server.js app.js; do
    if [ -f "$f" ]; then
      echo "➡️ Uruchamiam node $f"
      exec node "$f"
    fi
  done
  echo "❌ Nie znaleziono skryptu 'start' ani typowego pliku wejściowego (index.js, server.js, itp.)."
  exit 1
fi
