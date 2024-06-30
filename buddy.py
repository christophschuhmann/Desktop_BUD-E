# Import necessary libraries
import pvporcupine  # For wake word detection
import pvrecorder  # For audio recording
import subprocess  # For running system commands
import os  # For environment variables and file operations
import signal  # For handling signals (not used in this script, but imported for potential use)
import asyncio  # For asynchronous programming
from dotenv import load_dotenv  # For loading environment variables
import shutil  # For file operations
import requests  # For making HTTP requests
import time  # For time-related functions
import threading


# Import LangChain components
from langchain_core.prompts import ChatPromptTemplate
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from langchain.memory import ConversationBufferMemory
from langchain.prompts import (
    ChatPromptTemplate,
    MessagesPlaceholder,
    SystemMessagePromptTemplate,
    HumanMessagePromptTemplate,
)
from langchain.chains import LLMChain

# Import Deepgram components
from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    LiveTranscriptionEvents,
    LiveOptions,
    Microphone,
)

# Load environment variables from .env file
load_dotenv()

# Define LanguageModelProcessor class
class LanguageModelProcessor:
    def __init__(self):
        # Initialize the language model (LLM)
        self.llm = ChatGroq(temperature=0, model_name="mixtral-8x7b-32768", groq_api_key=os.getenv("GROQ_API_KEY"))
        # Alternatively, use OpenAI models (commented out)
        # self.llm = ChatOpenAI(temperature=0, model_name="gpt-4-0125-preview", openai_api_key=os.getenv("OPENAI_API_KEY"))
        # self.llm = ChatOpenAI(temperature=0, model_name="gpt-3.5-turbo-0125", openai_api_key=os.getenv("OPENAI_API_KEY"))

        # Initialize conversation memory
        self.memory = ConversationBufferMemory(memory_key="chat_history", return_messages=True)

        # Load system prompt from file
        with open('system_prompt.txt', 'r') as file:
            system_prompt = file.read().strip()
        
        # Create chat prompt template
        self.prompt = ChatPromptTemplate.from_messages([
            SystemMessagePromptTemplate.from_template(system_prompt),
            MessagesPlaceholder(variable_name="chat_history"),
            HumanMessagePromptTemplate.from_template("{text}")
        ])

        # Create conversation chain
        self.conversation = LLMChain(
            llm=self.llm,
            prompt=self.prompt,
            memory=self.memory
        )

    def process(self, text):
        # Add user message to memory
        self.memory.chat_memory.add_user_message(text)

        # Record start time
        start_time = time.time()

        # Get response from LLM
        response = self.conversation.invoke({"text": text})
        
        # Record end time
        end_time = time.time()

        # Add AI response to memory
        self.memory.chat_memory.add_ai_message(response['text'])

        # Calculate elapsed time
        elapsed_time = int((end_time - start_time) * 1000)
        print(f"LLM ({elapsed_time}ms): {response['text']}")
        return response['text']

# Define TextToSpeech class

class TextToSpeech:
    DG_API_KEY = os.getenv("DEEPGRAM_API_KEY")
    MODEL_NAME = "aura-helios-en"

    def __init__(self):
        self.player_process = None
        self.should_stop = False

    @staticmethod
    def is_installed(lib_name: str) -> bool:
        lib = shutil.which(lib_name)
        return lib is not None

    def stop(self):
        self.should_stop = True
        if self.player_process:
            self.player_process.terminate()
            self.player_process = None

    def speak(self, text, stop_event):
        if not self.is_installed("ffplay"):
            raise ValueError("ffplay not found, necessary to stream audio.")

        DEEPGRAM_URL = f"https://api.deepgram.com/v1/speak?model={self.MODEL_NAME}&performance=some&encoding=linear16&sample_rate=24000"
        headers = {
            "Authorization": f"Token {self.DG_API_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "text": text
        }

        player_command = ["ffplay", "-autoexit", "-", "-nodisp"]
        self.player_process = subprocess.Popen(
            player_command,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        start_time = time.time()
        first_byte_time = None

        try:
            with requests.post(DEEPGRAM_URL, stream=True, headers=headers, json=payload) as r:
                for chunk in r.iter_content(chunk_size=1024):
                    if stop_event.is_set() or self.should_stop:
                        break
                    if chunk:
                        if first_byte_time is None:
                            first_byte_time = time.time()
                            ttfb = int((first_byte_time - start_time)*1000)
                            print(f"TTS Time to First Byte (TTFB): {ttfb}ms\n")
                        try:
                            self.player_process.stdin.write(chunk)
                            self.player_process.stdin.flush()
                        except BrokenPipeError:
                            print("TTS playback stopped.")
                            break
        finally:
            if self.player_process and self.player_process.stdin:
                self.player_process.stdin.close()
            if self.player_process:
                self.player_process.wait()
            self.player_process = None

# Define TranscriptCollector class
class TranscriptCollector:
    def __init__(self):
        self.reset()

    def reset(self):
        # Reset transcript parts
        self.transcript_parts = []

    def add_part(self, part):
        # Add a part to the transcript
        self.transcript_parts.append(part)

    def get_full_transcript(self):
        # Get the full transcript
        return ' '.join(self.transcript_parts)

# Create a global transcript collector instance
transcript_collector = TranscriptCollector()

# Define get_transcript function
async def get_transcript(callback):
    transcription_complete = asyncio.Event()  # Event to signal transcription completion

    try:
        # Set up Deepgram client
        config = DeepgramClientOptions(options={"keepalive": "true"})
        deepgram: DeepgramClient = DeepgramClient("", config)

        dg_connection = deepgram.listen.asynclive.v("1")
        print("Listening...")

        async def on_message(self, result, **kwargs):
            sentence = result.channel.alternatives[0].transcript
            
            if not result.speech_final:
                transcript_collector.add_part(sentence)
            else:
                # This is the final part of the current sentence
                transcript_collector.add_part(sentence)
                full_sentence = transcript_collector.get_full_transcript()
                if len(full_sentence.strip()) > 0:
                    full_sentence = full_sentence.strip()
                    print(f"Human: {full_sentence}")
                    callback(full_sentence)  # Call the callback with the full_sentence
                    transcript_collector.reset()
                    transcription_complete.set()  # Signal to stop transcription and exit

        # Set up Deepgram connection event handler
        dg_connection.on(LiveTranscriptionEvents.Transcript, on_message)

        # Set up Deepgram live options
        options = LiveOptions(
            model="nova-2",
            punctuate=True,
            language="en-US",
            encoding="linear16",
            channels=1,
            sample_rate=16000,
            endpointing=300,
            smart_format=True
            
        )

        # Start Deepgram connection
        await dg_connection.start(options)

        # Open a microphone stream on the default input device
        microphone = Microphone(dg_connection.send)
        microphone.start()

        # Wait for the transcription to complete
        await transcription_complete.wait()

        # Wait for the microphone to close
        microphone.finish()

        # Indicate that we've finished
        await dg_connection.finish()

    except Exception as e:
        print(f"Could not open socket: {e}")
        return

# Define ConversationManager class
class ConversationManager:
    def __init__(self, porcupine, recorder):
        self.transcription_response = ""
        self.llm = LanguageModelProcessor()
        self.tts = TextToSpeech()
        self.porcupine = porcupine
        self.recorder = recorder
        self.stop_event = asyncio.Event()
        self.conversation_active = False

    async def listen_for_wake_words(self):
        while self.conversation_active:
            frames = self.recorder.read()
            keyword_index = self.porcupine.process(frames)
            if keyword_index == 1:  # "Stop Buddy" detected
                print("Wake word 'Stop Buddy' detected!")
                self.stop_event.set()
                self.tts.stop()
                break
            await asyncio.sleep(0.01)  # Small delay to allow other tasks to run

    async def speak_response(self, response):
        self.recorder.start()  # Ensure recorder is started
        tts_task = asyncio.to_thread(self.tts.speak, response, self.stop_event)
        wake_word_task = asyncio.create_task(self.listen_for_wake_words())
        
        try:
            await tts_task
        except Exception as e:
            print(f"TTS error: {e}")
        finally:
            wake_word_task.cancel()
            self.recorder.stop()  # Stop recorder after TTS

    async def main(self):
        def handle_full_sentence(full_sentence):
            self.transcription_response = full_sentence

        self.conversation_active = True
        while self.conversation_active:
            self.stop_event.clear()
            self.tts = TextToSpeech()  # Create a new TTS instance for each response
            
            print("Listening for your command...")
            self.recorder.start()
            await get_transcript(handle_full_sentence)
            self.recorder.stop()
            
            if "goodbye" in self.transcription_response.lower():
                self.conversation_active = False
                break
            
            llm_response = self.llm.process(self.transcription_response)
            print(f"AI: {llm_response}")

            await self.speak_response(llm_response)

            if self.stop_event.is_set():
                print("TTS was interrupted. Ready for next command.")
            
            self.transcription_response = ""

        print("Conversation ended. Listening for wake words again...")

async def main():
    access_key = "HsBjNtt2cDsNbbaFIBeEXcCTxkv8XrnDeRiuhtNz4EX5PmeAr1pOkQ=="  # Replace with your Picovoice AccessKey
    model_path = "hey-buddy_en_linux_v3_0_0.ppn"
    model2_path = "stop-buddy_en_linux_v3_0_0.ppn"

    porcupine = pvporcupine.create(access_key=access_key, keyword_paths=[model_path, model2_path])

    print("Listening for wake word 'Hey Buddy'...")

    while True:
        try:
            recorder = pvrecorder.PvRecorder(device_index=-1, frame_length=porcupine.frame_length)
            recorder.start()

            conversation_manager = ConversationManager(porcupine, recorder)

            while True:
                frames = recorder.read()
                keyword_index = porcupine.process(frames)
                if keyword_index == 0:  # "Hey Buddy" detected
                    print("Wake word 'Hey Buddy' detected!")
                    await conversation_manager.main()
                    print("Conversation ended. Listening for wake word 'Hey Buddy' again...")
                    break  # Break the inner loop to create a new recorder

        except KeyboardInterrupt:
            print("Stopping...")
            break

        finally:
            recorder.stop()
            recorder.delete()

    porcupine.delete()

# Entry point of the script
if __name__ == "__main__":
    asyncio.run(main())