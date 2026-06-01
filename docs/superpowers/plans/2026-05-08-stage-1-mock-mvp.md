# Stage 1 Mock MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a FastAPI mock live-translation microservice with a temporary demo website, WebSocket captions, SQLite persistence, mock providers, and tests.

**Architecture:** The backend is split by contracts: lesson API, Zoom API/RTMS placeholders, audio source, STT provider, translation provider, realtime delivery, persistence, and demo web UI. The demo frontend is only a client of the same HTTP/WebSocket surface that a future C# site will use.

**Tech Stack:** Python 3.11, FastAPI, Uvicorn, SQLAlchemy, SQLite, Jinja2, Pydantic Settings, pytest, httpx/TestClient, Docker.

---

## Tasks

- [ ] Create architecture docs and project scaffolding.
- [ ] Add failing tests for provider factories, mock providers, caption hub, audio pipeline, and WebSocket integration.
- [ ] Implement configuration, database models, repositories, schemas, and app lifecycle.
- [ ] Implement mock Zoom, audio source, STT, translator, pipeline, sessions, and WebSocket hub.
- [ ] Implement HTTP API and temporary web pages.
- [ ] Add README, `.env.example`, Dockerfile, docker-compose, and verification commands.

