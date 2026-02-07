.PHONY: up down up-bot up-whisper build logs

up:
	docker compose up -d --build

down:
	docker compose down

up-bot:
	docker compose up -d --build bot

up-whisper:
	docker compose up -d --build whisper

build:
	docker compose build

logs:
	docker compose logs -f

logs-bot:
	docker compose logs -f bot

logs-whisper:
	docker compose logs -f whisper
