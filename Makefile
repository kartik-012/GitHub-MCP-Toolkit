.PHONY: help install test eval run docker-build docker-up clean
   
help: 
	@echo "GitHub MCP Toolkit — Available Commands:"
	@echo "  make install      Install Python dependencies"
	@echo "  make test         Run 53 PyTest unit & integration tests"
	@echo "  make eval         Run 100-query dual evaluation benchmark"
	@echo "  make run          Start MCP server in stdio mode (Claude Desktop)"
	@echo "  make run-sse      Start MCP server in SSE mode (HTTP port 8000)"
	@echo "  make docker-build Build Docker container image"
	@echo "  make docker-up    Launch container stack with Docker Compose"
	@echo "  make clean        Remove cache and log runtime artifacts"

install:
	pip install -r requirements.txt

test:
	pytest tests/ -v

eval:
	python eval/run_eval.py

run:
	python server.py

run-sse:
	MCP_TRANSPORT=sse python server.py

docker-build:
	docker build -t github-mcp-toolkit:latest .

docker-up:
	docker compose up -d

clean:
	rm -rf .pytest_cache __pycache__ tools/__pycache__ tests/__pycache__ tool_calls.log traces.jsonl
