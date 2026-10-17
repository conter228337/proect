FROM mcr.microsoft.com/vscode/devcontainers/base:ubuntu

USER vscode
WORKDIR /workspaces

CMD ["sleep", "infinity"]
