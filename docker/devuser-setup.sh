#!/usr/bin/env bash
# Per-user dotfile setup, run as the dev user at every container start. Safe to
# run repeatedly: each block guards against re-appending the same content.
#
# The login `cd` target is taken from $DEVENV_WORKSPACE (default
# /workspace/repo), which the container launcher passes through at run time.
set -e

# .vimrc
if ! [ -f ~/.vimrc ]; then
  cat << 'EOF' > ~/.vimrc
set expandtab
set tabstop=2
set shiftwidth=2
map <C-j> <C-W>j
map <C-k> <C-W>k
map <C-h> <C-W>h
map <C-l> <C-W>l
EOF
fi

# .bashrc additions: git-branch prompt, ls colors, cd into the workspace on login.
if ! grep -q "# devenv-bashrc" ~/.bashrc 2>/dev/null; then
  cat << 'EOF' >> ~/.bashrc

# devenv-bashrc
__git_branch=""
__git_dirty=""
__git_staged=""
update_git_state() {
  if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    __git_branch=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
    __git_dirty=""
    if git diff --name-only 2>/dev/null | grep -q .; then __git_dirty="*"; fi
    __git_staged=""
    if git diff --cached --name-only 2>/dev/null | grep -q .; then __git_staged="+"; fi
  else
    __git_branch=""; __git_dirty=""; __git_staged=""
  fi
}
PS1='\[\033[01;32m\]\u@\h\[\033[00m\]:\[\033[01;34m\]\w\[\033[01;31m\]$(
  update_git_state
  if [ -n "$__git_branch" ]; then
    echo " ($__git_branch$([ -n "$__git_staged$__git_dirty" ] && echo " $__git_staged$__git_dirty"))"
  fi
)\[\033[00m\]\$ '

if [ -x /usr/bin/dircolors ]; then
  eval "$(dircolors -b)"
  alias ls='ls --color=auto'
  alias grep='grep --color=auto'
fi

cd "${DEVENV_WORKSPACE:-/workspace/repo}"
EOF
fi
