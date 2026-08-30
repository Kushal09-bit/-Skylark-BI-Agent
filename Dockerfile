FROM python:3.11-slim

# Node.js is required at runtime: the app spawns the official monday.com MCP
# server (`npx @mondaydotcomorg/monday-api-mcp`) as a subprocess for every
# board read. This is why Streamlit Community Cloud (Python-only) isn't used
# for hosting — see DECISION_LOG.md.
RUN apt-get update && apt-get install -y --no-install-recommends curl gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Pre-fetch the MCP server package into npm's cache so the first real request
# doesn't pay a cold npx-download penalty.
RUN npx --yes @mondaydotcomorg/monday-api-mcp --help >/dev/null 2>&1 || true

COPY . .

ENV PYTHONUNBUFFERED=1

CMD streamlit run app.py --server.port=${PORT:-8501} --server.address=0.0.0.0 --server.headless=true
