import asyncio
import pyaudio
from google import genai
from google.genai import types

# Your API Key
API_KEY = 'AIzaSyBlVrb87XJL4e9VoL_9wb8A0nYTEhHRAVc'

# Audio Specs: 16-bit PCM at 24kHz Mono
FORMAT = pyaudio.paInt16
CHANNELS = 1
RATE = 24000

client = genai.Client(
    api_key=API_KEY,
    http_options=types.HttpOptions(api_version='v1alpha')
)

async def test_gemini_31_voice():
    model_id = 'models/gemini-3.1-flash-live-preview'
    
    # Initialize PyAudio
    p = pyaudio.PyAudio()
    stream = p.open(format=FORMAT, channels=CHANNELS, rate=RATE, output=True)

    # Add system_instruction to force the identity
    config = types.LiveConnectConfig(
        response_modalities=['AUDIO'],
        system_instruction=types.Content(
            parts=[types.Part(text="""
                You are Gemini 3.1 Flash Live Preview, the latest real-time AI model. 
                You are currently being tested by Nifli from Nuro 7 in Kerala. 
                Always identify yourself by your full name when asked.
            """)]
        ),
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name='Sulafat')
            )
        )
    )

    print(f'--- Connecting to {model_id} ---')
    try:
        async with client.aio.live.connect(model=model_id, config=config) as session:
            print('SUCCESS: Session established. Listen for the response...')

            await session.send_realtime_input(
                text="Hi there!Who are you, and where is this conversation happening?, are you speak wiht me in malayalam then tell me about kerala ,malappuram and tell about malappuram malayalam differnce compared to official malayalam, then what is the changes give me  more examples"
            )

            async for response in session.receive():
                if response.server_content and response.server_content.model_turn:
                    for part in response.server_content.model_turn.parts:
                        if part.inline_data:
                            # Play the audio bytes
                            stream.write(part.inline_data.data)
                        
                        if part.text:
                            print(f">>> MODEL SAID: {part.text}")
                
                if response.server_content and response.server_content.turn_complete:
                    print('--- Conversation Finished ---')
                    break
                        
    except Exception as e:
        print(f'ERROR: {e}')
    finally:
        stream.stop_stream()
        stream.close()
        p.terminate()

if __name__ == "__main__":
    asyncio.run(test_gemini_31_voice())