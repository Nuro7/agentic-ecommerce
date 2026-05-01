# WooAgent Developer Documentation

This document provides a comprehensive technical overview of the "**WooAgent - Agentic Shopping Assistant for WooCommerce**" project. It is designed to help developers quickly understand the architecture, components, API interactions, and functionality of the system.

---

## 🏗 System Architecture

The project consists of two main overarching components that interact with each other to provide a voice-first UI shopping assistant for standard WooCommerce stores:

1. **WordPress/WooCommerce Plugin (`/wooagent/`)**: 
   A PHP-based plugin that injects a floating UI widget into the storefront and securely exposes specialized REST API endpoints for agent operations.
   
2. **FastAPI Backend (`/wooagent-backend/`)**:
   A Python FastAPI service that orchestrates the Large Language Model (LLM), external Speech-to-Text (STT), Text-to-Speech (TTS) tools, and the interaction logic between the LLM and the WooCommerce store APIs.

### The Flow of Data
1. Customer visits the WooCommerce store and sees the WooAgent Floating Assistant widget.
2. The widget (built in Vanilla JS, styled iteratively in CSS) collects User queries (text or voice recording).
3. The widget sends queries to the FastAPI Backend via its Chat/Transcription endpoints (`/api/v1/chat` or `/api/v1/transcribe`).
4. The Backend orchestrator processes the intent, consults the LLM, determines if a tool execution is needed (e.g. "search for blue shirts").
5. The Backend sends secure API calls back to the WordPress Plugin APIs to fulfill data requests or execute actions (e.g., fetching product inventory, adding to the cart).
6. The Backend formats a cohesive text-to-speech friendly message and returns the execution result + dialogue audio (or text) back to the Widget UI.

---

## 🧩 Component Breakdown: WordPress Plugin (`/wooagent/`)

The WordPress plugin essentially bridges WooCommerce functionality and makes it accessible for the standalone LLM Agent.

### Key Files and Functional Areas:
* **`wooagent.php`**: The main plugin entry. Initializes the API, Auth, and Settings classes and registers the scripts/styles.
* **`includes/class-wooagent-settings.php`**: Exposes the WooAgent settings page under WooCommerce > WooAgent. Defines required options:
  * Backend URL (Agent API URL)
  * Shared HMAC API Secret (used to secure endpoints)
  * UI customization parameters (Color, widget position, toggles)
* **`includes/class-wooagent-auth.php`**: Checks the authentication of incoming backend requests by validating the HMAC-based signature.
* **`includes/class-wooagent-api.php`**: Very critical. Exposes custom WordPress REST endpoints (`/wp-json/wooagent/v1/...`) that the Python Backend calls, effectively bypassing raw WooCommerce REST bottlenecks and adapting data specifically for prompting. Key endpoints:
  * `GET /session`: Syncs conversational state or reads current cart state.
  * `POST /cart/add` & `POST /cart/remove`: Cart operations abstracting Woo logic.
  * `GET /products/search` & `GET /products/{id}`: Formats product info compactly.
  * `GET /orders/email`: Checks the status of recent orders for order-tracking intents.
* **`widget/wooagent-widget.css` & `.js`**: The vanilla JavaScript widget injected into the storefront frontend that handles Voice recording, UI toggling, loading states, and HTTP communication with the Python Backend API. 

---

## 🧠 Component Breakdown: Agent Backend (`/wooagent-backend/`)

The Python FastAPI backend serves as the brain, performing Natural Language Processing, intent resolution, prompt configuration, and memory caching via Redis.

### Key Directories and Files:
* **`main.py`**: Entry point for FastAPI app, initializes the Redis connection, the core orchestrator, CORS middlewares, and spans out the HTTP routers.
* **`routers/`**:
  * `chat.py`: Handles POST `/api/v1/chat`. Passes conversation strings to the Orchestrator and relays the resulting JSON state/actions back.
  * `transcribe.py`: Handles audio transcription using the configured STT service.
  * `greet.py` / `health.py`: Ancillary interaction and connectivity tests.
* **`agent/orchestrator.py`**: The heavy lifter (~1800 lines of code!). Coordinates processing loops:
  * Uses fast fuzzy rule-based intent parsing (e.g., `_run_fast_intent`) to bypass expensive LLM calls if the user asks simple commands (like "add to cart"). 
  * Triggers the LLM (`_run_llm_agent`) equipped with tools when the prompt is complex (e.g., "Find me a red cotton shirt under $50").
  * Uses the "AddressCollectionState" flow to guide users gracefully in a deterministic step-by-step path for capturing delivery information via voice.
* **`agent/tools.py`**: Defines the JSON Schema definitions mapped to LLM tool bindings (`search_products`, `get_product_details`, `add_to_cart`, `check_inventory`, `check_order_status`). When the LLM outputs a tool call, this file maps it to the Python logic. 
* **`services/woocommerce.py`**: An HTTPX-based wrapper client that talks TO the WooCommerce endpoints (both standard `/wp-json/wc/v3` and the custom plugin endpoint `/wp-json/wooagent/v1`). Handles request signing using the shared secret.
* **`services/session.py`** & **`services/rate_limit.py`**: Manage user interaction history inside Redis and prevent endpoint abuse.
* **`services/stt.py`** & **`services/tts.py`**: Translates audio interactions utilizing providers like Groq, Anthropic, Deepgram, and ElevenLabs depending on the `.env` configuration.

---

## 🔄 Sequence Diagram: Adding a Product to Cart

Here is the typical interaction flow when a customer asks the agent to perform an action.

1. **User (Storefront)**: Clicks the microphone, says "Add the blue polo to my cart".
2. **Widget (.js)**: Captures audio, calls FastAPI POST `/transcribe`, gets back "Add the blue polo to my cart".
3. **Widget (.js)**: Calls FastAPI POST `/chat` with transcribed text and local User Session ID.
4. **FastAPI (Router)**: Passes data to `AgentOrchestrator`.
5. **AgentOrchestrator**: Checks user history in Redis. Uses Groq LLM to understand intent.
6. **LLM**: Emits a tool call: `search_products(query="blue polo")`.
7. **FastAPI (WooCommerce Client)**: Issues signed `GET /wp-json/wooagent/v1/products/search?query=blue polo` to WordPress.
8. **WordPress (`wooagent-api.php`)**: Queries Woo DB, returns compact payload of the blue polo (ID: 153).
9. **FastAPI (WooCommerce Client)**: Formats data & Orchestrator invokes `_handle_add_to_cart`.
10. **FastAPI (WooCommerce Client)**: Issues signed `POST /wp-json/wooagent/v1/cart/add { product_id: 153 }` to WordPress.
11. **WordPress (`wooagent-api.php`)**: Processes addition to the WC Session Cart, returns success JSON.
12. **AgentOrchestrator**: Drafts output phrase "I've added the blue polo to your cart!".
13. **Widget (.js)**: Receives the JSON response (with new Cart totals), plays TTS audio, and updates the cart UI instantly.

---

## 🛠 Developer Workflow & Local Environment

### Running Local Instance
To test development modifications on a local desktop:

1. Copy the `/wooagent/` folder into your `wp-content/plugins/` directory and activate the plugin inside WP.
2. In WordPress: WooCommerce > WooAgent -> Add API URL `http://localhost:8000` and create a `SHARED_SECRET` (e.g., `super-secret-key-123`).
3. For the backend setup, create `wooagent-backend/.env` copying from `.env.example`.
4. Run the backend stack via Docker Compose: `docker-compose up --build`.

*(Note: If WordPress is hosted securely `https` but the Backend runs locally, you MUST use `ngrok` to tunnel `8000` to an HTTPS endpoint, as browser mixed-content policies will block the Widget HTTP calls.)*

### Core Technology Stack
- **Languages**: PHP 8.x (Plugin) & Python 3.12 (Backend)
- **Database Backend**: Redis (Maintains short-lived dialogue states to avoid straining MySQL) + MySQL (Standard WP/Woo data)
- **Voice APIs**: Groq, Deepgram, Browser-Native TTS. 
- **Web APIs**: FastAPI (Async router), WP-REST (Sync hooks).

---

If you're modifying functionality:
* **Changing Prompts/AI Behavior**: Check `wooagent-backend/agent/prompts.py` & `orchestrator.py`
* **Adding a new widget capability (UI)**: Check `wooagent/widget/wooagent-widget.js`.
* **Exposing new WooCommerce Data**: You must first expose it in `wooagent/includes/class-wooagent-api.php` and then consume it in `wooagent-backend/services/woocommerce.py`.
