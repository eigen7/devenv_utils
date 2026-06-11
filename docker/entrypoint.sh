#!/usr/bin/env bash
# Container entrypoint: reconcile devuser's UID/GID with the host user's so
# that anything devuser writes into the bind-mounts lands on the host with sane
# ownership; then drop privileges and exec the user's command.
set -e

HOST_UID=${HOST_UID:-1000}
HOST_GID=${HOST_GID:-1000}
USERNAME=${USERNAME:-devuser}

# Group: create it at HOST_GID, or rename whatever group already has that GID.
if ! getent group "$USERNAME" >/dev/null; then
  if getent group "$HOST_GID" >/dev/null; then
    existing=$(getent group "$HOST_GID" | cut -d: -f1)
    groupmod -n "$USERNAME" "$existing"
  else
    groupadd -g "$HOST_GID" "$USERNAME"
  fi
fi

# User: same dance for HOST_UID.
if ! getent passwd "$USERNAME" >/dev/null 2>&1; then
  if getent passwd "$HOST_UID" >/dev/null 2>&1; then
    existing_user=$(getent passwd "$HOST_UID" | cut -d: -f1)
    usermod -l "$USERNAME" "$existing_user"
    usermod -d "/home/$USERNAME" -m "$USERNAME"
    usermod -g "$HOST_GID" "$USERNAME"
  else
    useradd -m -u "$HOST_UID" -g "$HOST_GID" -s /bin/bash "$USERNAME"
  fi
fi

# Passwordless sudo for convenience inside the container (sudoers.d so this
# is idempotent across container restarts), plus hardware-access groups.
mkdir -p /etc/sudoers.d
echo "$USERNAME ALL=(ALL) NOPASSWD:ALL" > "/etc/sudoers.d/$USERNAME"
chmod 0440 "/etc/sudoers.d/$USERNAME"
if getent group sudo >/dev/null; then usermod -aG sudo "$USERNAME"; fi
if getent group video >/dev/null; then usermod -aG video "$USERNAME"; fi

mkdir -p /workspace
chown "$USERNAME":"$USERNAME" /workspace

# Per-user dotfiles (idempotent), then exec the requested command as devuser.
gosu "$USERNAME" /usr/local/bin/devuser-setup.sh
exec gosu "$USERNAME" "$@"
