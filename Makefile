.PHONY: up down up-bot up-whisper build logs logs-bot logs-whisper

up down up-bot up-whisper logs:
	bash deploy.sh $@

build:
	./bin/innervoice build

logs-bot:
	./bin/innervoice logs bot

logs-whisper:
	./bin/innervoice logs whisper
