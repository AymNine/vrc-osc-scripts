"""
VRCSubs - A script to create "subtitles" for yourself using the VRChat textbox!
(c) 2022 CyberKitsune & other contributors.
"""

import queue, threading, datetime, os, time, textwrap
import speech_recognition as sr
import cyrtranslit
import unidecode
import pykakasi
from pythonnet import load
load("coreclr")
import clr
clr.AddReference(f"{os.path.dirname(os.path.realpath(__file__))}/vrc-oscquery-lib.dll")
from VRC.OSCQuery import OSCQueryService
from VRC.OSCQuery import Extensions

from googletrans import Translator
from speech_recognition import UnknownValueError, WaitTimeoutError, AudioData
from pythonosc import udp_client
from pythonosc.dispatcher import Dispatcher
from pythonosc.osc_server import BlockingOSCUDPServer
from yaml import load
try:
    from yaml import CLoader as Loader
except ImportError:
    from yaml import Loader


config = {'FollowMicMute': True, 'CapturedLanguage': "en-US", 'EnableTranslation': False, "TranslateTo": "en-US", 'AllowOSCControl': True, 'Pause': False, 'TranslateInterumResults': True, 'OSCControlPort': 9001}
state = {'selfMuted': False}
state_lock = threading.Lock()

r = sr.Recognizer()
audio_queue = queue.Queue()

'''
MISC HELPERS
This code is just to cleanup code blow
'''
def strip_dialect(langcode):
    # zh is the long langcode where we need to preserve
    langsplit = langcode.split('-')[0]
    if langsplit == "zh":
        if langcode == "zh-CN":
            return langcode
        return "zh-TW"
    return langsplit

'''
STATE MANAGEMENT
This should be thread safe
'''
def get_state(key):
    global state, state_lock
    state_lock.acquire()
    result = None
    if key in state:
        result = state[key]
    state_lock.release()
    return result

def set_state(key, value):
    global state, state_lock
    state_lock.acquire()
    state[key] = value
    state_lock.release()

'''
SOUND PROCESSING THREAD
'''
def process_sound():
    global audio_queue, r, config
    client = udp_client.SimpleUDPClient("127.0.0.1", 9000)
    current_text = ""
    last_text = ""
    last_disp_time = datetime.datetime.now()
    translator = Translator()
    print("[ProcessThread] Starting audio processing!")
    while True:
        if config["EnableTranslation"] and translator is None:
            #translator = Translator()
            print("[ProcessThread] Enabling Translation!")
        
        ad, final = audio_queue.get()

        if config["FollowMicMute"] and get_state("selfMuted"):
            continue

        if config["Pause"]:
            continue

        client.send_message("/chatbox/typing", (not final))

        if config["EnableTranslation"] and not config["TranslateInterumResults"] and not final:
            continue

        text = None
        
        time_now = datetime.datetime.now()
        difference = time_now - last_disp_time
        if difference.total_seconds() < 1 and not final:
            continue
        

        try:
            #client.send_message("/chatbox/typing", True)
            text = r.recognize_google(ad, language=config["CapturedLanguage"])
        except UnknownValueError:
            #client.send_message("/chatbox/typing", False)
            continue
        except TimeoutError:
            #client.send_message("/chatbox/typing", False)
            print("[ProcessThread] Timeout Error when recognizing speech!")
            continue
        except Exception as e:
            print("[ProcessThread] Exception!", e)
            #client.send_message("/chatbox/typing", False)
            continue

        current_text = text

        if last_text == current_text:
            continue

        last_text = current_text

        diff_in_milliseconds = difference.total_seconds() * 1000
        if diff_in_milliseconds < 1500:
            ms_to_sleep = 1500 - diff_in_milliseconds
            print("[ProcessThread] Sending too many messages! Delaying by", (ms_to_sleep / 1000.0), "sec to not hit rate limit!")
            time.sleep(ms_to_sleep / 1000.0)

        
        textDispLangage = config["CapturedLanguage"]
        if config["EnableTranslation"]:
            try:
                trans = translator.translate(src=strip_dialect(config["CapturedLanguage"]), dest=strip_dialect(config["TranslateTo"]), text=current_text)
                current_text = trans.text + " [%s->%s]" % (config["CapturedLanguage"], config["TranslateTo"])
                textDispLangage = config["TranslateTo"]
                print("[ProcessThread] Recognized:",trans.origin, "->", current_text)
            except Exception as e:
                print("[ProcessThread] Translating ran into an error!", e)
        else:
            print("[ProcessThread] Recognized:",current_text)

        if len(current_text) > 144:
            current_text = textwrap.wrap(current_text, width=144)[-1]

        last_disp_time = datetime.datetime.now()
       

        textChanged = False

        if textDispLangage == "ru-RU":
            textChanged = True
            current_text = cyrtranslit.to_latin(current_text, "ru")
        elif textDispLangage == "uk-UA":
            textChanged = True
            current_text = cyrtranslit.to_latin(current_text, "ua")
        elif strip_dialect(textDispLangage) == "ja":
            textChanged = True
            kks = pykakasi.kakasi()
            conv = kks.convert(current_text)
            current_text = ' '.join([part['hepburn'] for part in conv])
        elif not current_text.isascii():
            textChanged = True
            current_text = unidecode.unidecode_expect_nonascii(current_text)

        if textChanged:
            print("[ProcessThread] Converted to ascii:", current_text)

        client.send_message("/chatbox/input", [current_text, True])

'''
AUDIO COLLECTION THREAD
'''
def collect_audio():
    global audio_queue, r, config
    mic = sr.Microphone()
    print("[AudioThread] Starting audio collection!")
    did = mic.get_pyaudio().PyAudio().get_default_input_device_info()
    print("[AudioThread] Using", did.get('name'), "as Microphone!")
    with mic as source:
        audio_buf = None
        buf_size = 0
        while True:
            audio = None
            try:
                audio = r.listen(source, phrase_time_limit=1, timeout=0.1)
            except WaitTimeoutError:
                if audio_buf is not None:
                    audio_queue.put((audio_buf, True))
                    audio_buf = None
                    buf_size = 0
                continue

            if audio is not None:
                if audio_buf is None:
                    audio_buf = audio
                else:
                    buf_size += 1
                    if buf_size > 10:
                        audio_buf = audio
                        buf_size = 0
                    else:
                        audio_buf = AudioData(audio_buf.frame_data + audio.frame_data, audio.sample_rate, audio.sample_width)
                    
                audio_queue.put((audio_buf, False))
                   

'''
OSC BLOCK
TODO: This maybe should be bundled into a class
'''
class OSCServer():
    def __init__(self):
        global config
        self.osc_port = Extensions.GetAvailableUdpPort()
        self.http_port = Extensions.GetAvailableTcpPort()
        self.oscquery = OSCQueryService("VRCSubs-%i" % self.osc_port, self.http_port, self.osc_port, None)
        print("[OSCQuery] Running on HTTP port", self.http_port, "and UDP port", self.osc_port)

        self.dispatcher = Dispatcher()
        self.dispatcher.set_default_handler(self._def_osc_dispatch)
        self.dispatcher.map("/avatar/parameters/MuteSelf", self._osc_muteself)

        for key in config.keys():
            self.dispatcher.map("/avatar/parameters/vrcsub-%s" % key, self._osc_updateconf)

        self.server = BlockingOSCUDPServer(("127.0.0.1", self.osc_port), self.dispatcher)
        self.server_thread = threading.Thread(target=self._process_osc)

        

    def launch(self):
        self.server_thread.start()

    def shutdown(self):
        self.server.shutdown()
        self.server_thread.join()

    def _osc_muteself(self, address, *args):
        print("[OSCThread] Mute is now", args[0])
        set_state("selfMuted", args[0])

    def _osc_updateconf(self, address, *args):
        key = address.split("vrcsub-")[1]
        print("[OSCThread]", key, "is now", args[0])
        config[key] = args[0]

    def _def_osc_dispatch(self, address, *args):
        pass
        #print(f"{address}: {args}")

    def _process_osc(self):
        print("[OSCThread] Launching OSC server thread!")
        self.server.serve_forever()


'''
MAIN ROUTINE
'''
def main():
    global config
    # Load config
    cfgfile = f"{os.path.dirname(os.path.realpath(__file__))}/Config.yml"
    if os.path.exists(cfgfile):
        print("[VRCSubs] Loading config from", cfgfile)
        with open(cfgfile, 'r') as f:
            config = load(f, Loader=Loader)

    # Start threads
    pst = threading.Thread(target=process_sound)
    pst.start()

    cat = threading.Thread(target=collect_audio)
    cat.start()
    
    osc = None
    launchOSC = False
    if config['FollowMicMute']:
        print("[VRCSubs] FollowMicMute is enabled in the config, speech recognition will pause when your mic is muted in-game!")
        launchOSC = True
    else:
        print("[VRCSubs] FollowMicMute is NOT enabled in the config, speech recognition will work even while muted in-game!")

    if config['AllowOSCControl']:
        print("[VRCSubs] AllowOSCControl is enabled in the config, will listen for OSC controls!")
        launchOSC = True

    if launchOSC:
        osc = OSCServer()
        osc.launch()

    pst.join()
    cat.join()
    
    if osc is not None:
        osc.shutdown()

if __name__ == "__main__":   
    main()