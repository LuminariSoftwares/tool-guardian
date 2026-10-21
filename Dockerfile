# Tool Guardian as a stdio MCP server. Pure standard library: nothing but the package is installed.
# The servers it fronts run inside this container too, so a backend needing npx or uvx needs an image
# that has them; with no config it starts with zero backends and lists its router tools.
#
#   docker build -t tool-guardian .
#   docker run -i --rm -v "$HOME/.tool-guardian:/home/guardian/.tool-guardian" tool-guardian
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY tool_guardian.py tg_*.py ./
RUN pip install --no-cache-dir . && rm -rf /app/build \
 && useradd --create-home --uid 10001 guardian
# Config, state and spill files resolve under ~/.tool-guardian, i.e. /home/guardian/.tool-guardian.
USER guardian
WORKDIR /home/guardian
ENTRYPOINT ["tool-guardian"]
