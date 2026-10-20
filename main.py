import os
from pathlib import Path
import json
import logging
import asyncio
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Response, Form
from dotenv import load_dotenv
import openai

from mcp_server import mcp, lookup_employee, transfer_call
from vector_store import VectorStore

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("calling_agent")

app = FastAPI(title="Calling Agent Backend")

# Mount MCP Server SSE App on /mcp
# FastMCP provides sse_app() which exposes /sse and /messages routes.
app.mount("/mcp", mcp.sse_app())

# Initialize Vector Store
db = VectorStore()

openai_client = openai.AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

async def get_twilio_call_sid(call_id: str) -> str:
    """
    Fetches the Retell call session and extracts the Twilio Call SID.
    """
    api_key = os.getenv("RETELL_API_KEY")
    if not api_key:
        logger.error("RETELL_API_KEY is not set.")
        return None
        
    url = f"https://api.retellai.com/v2/get-call/{call_id}"
    headers = {
        "Authorization": f"Bearer {api_key}"
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers)
            if response.status_code == 200:
                data = response.json()
                # Check retell_llm_dynamic_variables or telephony_identifier
                dyn_vars = data.get("retell_llm_dynamic_variables", {})
                if isinstance(dyn_vars, dict) and "twilio-callsid" in dyn_vars:
                    return dyn_vars["twilio-callsid"]
                tel_id = data.get("telephony_identifier", {})
                if isinstance(tel_id, dict) and "twilio_call_sid" in tel_id:
                    return tel_id["twilio_call_sid"]
    except Exception as ex:
        logger.error(f"Failed to fetch Call SID from Retell: {ex}")
    return None

async def get_parent_call_info(child_call_sid: str) -> tuple[str, str]:
    """
    Fetches Twilio call details and returns (parent_call_sid, twilio_number).
    If parent_call_sid doesn't exist, returns (child_call_sid, twilio_number).
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        logger.error("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is not set.")
        return child_call_sid, ""
        
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{child_call_sid}.json"
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(url, auth=(account_sid, auth_token))
            if response.status_code == 200:
                data = response.json()
                parent_sid = data.get("parent_call_sid")
                
                if parent_sid:
                    # We found a parent SID. Let's fetch the parent call to get its 'to' number (the Twilio number).
                    parent_url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{parent_sid}.json"
                    parent_response = await client.get(parent_url, auth=(account_sid, auth_token))
                    if parent_response.status_code == 200:
                        parent_data = parent_response.json()
                        twilio_number = parent_data.get("to")
                        logger.info(f"Found parent call SID {parent_sid} (To: {twilio_number}) for child call SID {child_call_sid}")
                        return parent_sid, twilio_number
                    else:
                        logger.error("Failed to fetch parent call details for To number.")
                        return parent_sid, ""
                else:
                    twilio_number = data.get("to")
                    return child_call_sid, twilio_number
            else:
                logger.error(f"Failed to fetch Twilio call details for parent SID resolution: status {response.status_code}, response: {response.text}")
    except Exception as ex:
        logger.error(f"Error fetching parent call SID from Twilio: {ex}")
    return child_call_sid, ""

async def redirect_twilio_call(call_sid: str, target_phone_number: str, caller_id: str = "") -> bool:
    """
    Updates the active Twilio call dynamically using TwiML to redirect/dial the target number.
    Uses caller_id to prevent Dial failures when the original caller ID isn't verified.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    if not account_sid or not auth_token:
        logger.error("TWILIO_ACCOUNT_SID or TWILIO_AUTH_TOKEN is not set.")
        return False
        
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Calls/{call_sid}.json"
    
    caller_id_attr = f' callerId="{caller_id}"' if caller_id else ""
    # Simple Dial TwiML response
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial{caller_id_attr}>{target_phone_number}</Dial>
</Response>"""
    
    payload = {
        "Twiml": twiml
    }
    
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                data=payload,
                auth=(account_sid, auth_token)
            )
            if response.status_code == 200:
                logger.info(f"Successfully redirected Twilio call {call_sid} to {target_phone_number}")
                return True
            else:
                logger.error(f"Failed to redirect Twilio call: status {response.status_code}, response: {response.text}")
    except Exception as ex:
        logger.error(f"Error redirecting Twilio call: {ex}")
    return False

async def delayed_redirect(call_id: str, target_number: str, delay_seconds: float = 2.0):
    """
    Asynchronously waits for a delay to allow the LLM response TTS to finish,
    then fetches the call details and redirects the parent call leg to the target number.
    """
    logger.info(f"delayed_redirect started. Waiting {delay_seconds}s before redirecting call_id: {call_id} to {target_number}...")
    await asyncio.sleep(delay_seconds)
    
    twilio_sid = await get_twilio_call_sid(call_id)
    if not twilio_sid:
        logger.error(f"Could not retrieve Twilio Call SID for redirection of call_id: {call_id}")
        return
        
    redirect_sid, caller_id = await get_parent_call_info(twilio_sid)
    logger.info(f"Redirecting Twilio call SID: {redirect_sid} to {target_number} with callerId {caller_id}...")
    success = await redirect_twilio_call(redirect_sid, target_number, caller_id)
    if success:
        logger.info(f"Successfully redirected call leg {redirect_sid} to {target_number}")
    else:
        logger.error(f"Redirection failed for call leg {redirect_sid}")


PROMPTS_DIR = Path(__file__).parent / "prompts"

def load_prompt(name: str) -> str:
    return (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8").strip()

SYSTEM_PROMPT = load_prompt("system_prompt")
GREETING_TEXT = load_prompt("greeting")
ERROR_TEXT = load_prompt("error_message")

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/twilio-voice")
def twilio_voice(To: str = Form(...)):
    """
    Twilio voice webhook. Returns TwiML dial instruction routing calls to Retell AI SIP endpoint.
    """
    logger.info(f"Received call on Twilio number: {To}")
    
    # If the number starts with '+' (E.164), prepend it to the Retell SIP URI
    sip_target = "sip.retellai.com"
    if To and To.startswith("+"):
        sip_target = f"{To}@sip.retellai.com"
        
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Sip>sip:{sip_target};transport=tcp</Sip>
    </Dial>
</Response>"""
    return Response(content=twiml, media_type="application/xml")





@app.post("/populate-db")
def populate_db(employees: list = None):
    """
    Populates ChromaDB with mock employee records.
    """
    # Delete existing records in the database
    db.clear_database()
    
    if not employees:
        # Default mock data
        employees = [
            {
                "name": "Shubham",
                "designation": "Senior Software Associate",
                "phone_number": "+917018593743",
                "team": "Python Team",
                "technology": "Python, Django, FastAPI, Machine Learning"
            },
            {
                "name": "Abhishek",
                "designation": "Junior Software Associate",
                "phone_number": "+918580759903",
                "team": "Python Team",
                "technology": "Python, Flask, SQL"
            }
        ]
    
    added_ids = []
    for emp in employees:
        doc_id = db.add_employee(
            name=emp["name"],
            designation=emp["designation"],
            phone_number=emp["phone_number"],
            team=emp["team"],
            technology=emp["technology"]
        )
        added_ids.append(doc_id)
        
    return {"message": "Success", "added_ids": added_ids}


# Retell WebSocket LLM Handler
@app.websocket("/llm-websocket/{call_id}")
async def llm_websocket(websocket: WebSocket, call_id: str):
    await websocket.accept()
    logger.info(f"Retell WebSocket connected for call: {call_id}")
    
    # Send initial greeting
    greeting_text = GREETING_TEXT
    await websocket.send_json({
        "response_type": "response",
        "response_id": 0,
        "content": greeting_text,
        "content_complete": True
    })
    
    try:
        while True:
            # Receive event from Retell
            data = await websocket.receive_json()
            logger.info(f"Received message from Retell: {data}")
            
            interaction_type = data.get("interaction_type")
            
            if interaction_type == "response_required":
                response_id = data.get("response_id", 0)
                transcript = data.get("transcript", [])
                
                # Build OpenAI messages from the transcript
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                for turn in transcript:
                    role = turn.get("role")
                    content = turn.get("content", "")
                    if role == "agent":
                        role = "assistant"
                    elif role != "user":
                        role = "user"
                    messages.append({"role": role, "content": content})
                
                # Generate OpenAI completion with tools
                transfer_number = None
                ai_response_content = ""
                
                try:
                    tools_config = [
                        {
                            "type": "function",
                            "function": {
                                "name": "lookup_employee",
                                "description": "Looks up an employee's contact details (phone number, team, designation) using their name and/or team name.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string", "description": "The name of the employee to search for."},
                                        "team": {"type": "string", "description": "The team they work in (optional)."}
                                    },
                                    "required": ["name"]
                                }
                            }
                        },
                        {
                            "type": "function",
                            "function": {
                                "name": "transfer_call",
                                "description": "Initiates a call transfer to the specified phone number in E.164 format.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "phone_number": {"type": "string", "description": "The phone number to transfer to (e.g. +1234567890)."}
                                    },
                                    "required": ["phone_number"]
                                }
                            }
                        }
                    ]
                    
                    # Call OpenAI loop to resolve potential tool calls
                    run_llm = True
                    openai_messages = list(messages)
                    
                    while run_llm:
                        response = await openai_client.chat.completions.create(
                            model="gpt-4o-mini",
                            messages=openai_messages,
                            tools=tools_config,
                            tool_choice="auto"
                        )
                        
                        choice = response.choices[0]
                        message = choice.message
                        
                        if message.content:
                            ai_response_content = message.content
                        
                        if message.tool_calls:
                            # Append the assistant tool request message
                            openai_messages.append(message)
                            
                            for tool_call in message.tool_calls:
                                name = tool_call.function.name
                                args = json.loads(tool_call.function.arguments)
                                
                                logger.info(f"LLM requested tool call: {name} with args {args}")
                                
                                # Execute local tool
                                if name == "lookup_employee":
                                    result_str = lookup_employee(
                                        name=args.get("name"),
                                        team=args.get("team")
                                    )
                                elif name == "transfer_call":
                                    result_str = transfer_call(
                                        phone_number=args.get("phone_number")
                                    )
                                    target_number = args.get("phone_number")
                                    
                                    # Perform direct Twilio redirection in background with a 2-second delay
                                    logger.info(f"Scheduling direct Twilio call redirect to {target_number} in 2 seconds...")
                                    asyncio.create_task(delayed_redirect(call_id, target_number, 2.0))
                                    
                                    # Mark that we should perform transfer
                                    transfer_number = target_number
                                else:
                                    result_str = "Error: Unknown tool."
                                
                                # Append tool response message
                                openai_messages.append({
                                    "role": "tool",
                                    "tool_call_id": tool_call.id,
                                    "name": name,
                                    "content": result_str
                                })
                            
                            # Continue loop to let model formulate response based on tool execution
                            continue
                        else:
                            # No tool calls, we are done
                            run_llm = False
                    
                    # Prepare final response to Retell
                    websocket_response = {
                        "response_type": "response",
                        "response_id": response_id,
                        "content": ai_response_content,
                        "content_complete": True
                    }
                    
                    try:
                        await websocket.send_json(websocket_response)
                        logger.info(f"Sent response to Retell: {websocket_response}")
                    except Exception as ws_err:
                        logger.info(f"WebSocket send skipped/closed during call transition: {ws_err}")
                    
                except Exception as ex:
                    logger.error(f"Error invoking OpenAI/Tools: {ex}")
                    # Send error recovery response
                    try:
                        await websocket.send_json({
                            "response_type": "response",
                            "response_id": response_id,
                            "content": ERROR_TEXT,
                            "content_complete": True
                        })
                    except Exception:
                        pass
            
            elif interaction_type == "update_only":
                # Retell sent update, no response needed
                logger.info(f"Transcript update: {data.get('transcript')}")
                
    except WebSocketDisconnect:
        logger.info(f"Retell WebSocket disconnected for call: {call_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
