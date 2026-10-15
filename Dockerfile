FROM python:3.12-slim

WORKDIR /app

# Create non-root runtime user for least-privilege container security
RUN groupadd -r mcpuser && useradd -r -g mcpuser -d /app -s /sbin/nologin mcpuser

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and transfer ownership to non-root user
COPY --chown=mcpuser:mcpuser . .

# Environment defaults
ENV PYTHONUNBUFFERED=1
ENV MCP_TRANSPORT=sse
ENV MCP_AGENT_ROLE=admin

EXPOSE 8000

USER mcpuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "from eval.run_eval import verify_live_server_parity; assert verify_live_server_parity()['parity_ok']" || exit 1

CMD ["python", "server.py"]
