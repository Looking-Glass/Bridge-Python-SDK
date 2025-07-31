#!/usr/bin/env python3
"""
GUI for Flux Server using Dear ImGui + GLFW + BridgeAPI

This GUI only generates RGBD images. Users can specify a text prompt and adjust
the focus and depthiness parameters. The resulting RGBD texture is drawn to a
Looking Glass display via BridgeAPI, and the color portion plus the full RGBD
image is previewed in a single fullscreen ImGui window with split panels.
A “Random Prompt” button selects from a small library of example prompts.
A “Generate Prompt” button fetches a themed prompt via the Flux-Server API.
Looping modes generate random or themed prompts asynchronously without freezing the UI.
Saves every generated RGBD image (and its prompt) into out/metadata.json via ImageManager,
preloads only as needed, and lets you navigate back and forth through saved images.
"""

import sys
import os
import base64
import requests
import random
import threading
import queue
import time
import json
from io import BytesIO
from typing import Callable
import uuid

import imgui
from imgui.integrations.glfw import GlfwRenderer
import glfw
from PIL import Image
from OpenGL import GL

# Add the parent directory to the Python path for BridgeApi
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from BridgeApi import BridgeAPI, PixelFormats  # type: ignore

class RandomPromptGenerator:
    def __init__(self, config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        self.adjectives = data['adjectives']
        self.colors = data['colors']
        self.subjects = data['subjects']
        self.actions = data['actions']
        self.two_subject_actions = set(data['twoSubjectActions'])
        self.environments = data['environments']
        self.weather = data['weather']
        self.styles = data['styles']
        self.descriptors = data['descriptors']

        # Synonyms / variants
        self.openers = ["Imagine", "Picture", "Behold", "Envision", "Witness"]
        self.env_preps = ["amid", "within", "beneath", "against", "surrounded by"]
        self.weather_preps = ["under", "with", "against a backdrop of", "beneath", "amidst"]
        self.descriptor_synonyms = {
            d: [d, f"featuring {d}", f"highlighting {d}"] for d in self.descriptors
        }
        self.style_synonyms = {
            s: [s, f"{s} style", f"in {s}"] for s in self.styles
        }

        # Expanded sentence templates
        self.templates = [
            "{opener}! {article} {head} {action_phrase} {env_clause} {weather_clause}, {desc_clause}, {style}.",
            "{article} {head} {action_phrase} {env_clause} and {weather_clause}; {desc_clause}. Style: {style}.",
            "{opener}? A {head} {action_phrase}, {weather_clause} {env_clause}, featuring {desc_clause} in {style}.",
            "{article} {head} {action_phrase} {weather_clause} {env_clause}, showcasing {desc_clause}, rendered in {style}.",
            "{opener}! {article} {head} {action_phrase}, {desc_clause}, {env_clause} {weather_clause}. {style}.",
            "{article} {head} {action_phrase}. Scene: {env_clause} with {weather_clause}, highlighting {desc_clause} in {style}."
        ]

    def GetNextRandomPrompt(self):
        # Adjectives + optional color
        num_adjs = random.randint(1, 3)
        adjs = random.sample(self.adjectives, k=num_adjs)
        if random.random() < 0.6:
            adjs.insert(random.randrange(len(adjs) + 1), random.choice(self.colors))
        head = " ".join(adjs + [random.choice(self.subjects)])
        article = "An" if head[0].lower() in "aeiou" else "A"

        # Action + optional second subject
        base_act = random.choice(self.actions)
        if base_act in self.two_subject_actions and random.random() < 0.4:
            other = random.choice([s for s in self.subjects if s not in head])
            action_phrase = f"{random.choice([base_act, base_act + 'ing'])} {other}"
        else:
            action_phrase = random.choice([base_act, base_act + "ing"])

        # Environment & weather with synonyms
        env = random.choice(self.environments)
        weather = random.choice(self.weather)
        env_clause = f"{random.choice(self.env_preps)} {env}"
        weather_clause = f"{random.choice(self.weather_preps)} {weather}"

        # Descriptors & styles with synonyms
        num_desc = random.randint(1, 3)
        chosen_descs = random.sample(self.descriptors, k=num_desc)
        desc_clause = "; ".join(
            random.choice(self.descriptor_synonyms[d]) for d in chosen_descs
        )
        style_key = random.choice(self.styles)
        style = random.choice(self.style_synonyms[style_key])

        # Optional opener
        opener = random.choice(self.openers) if random.random() < 0.5 else ""

        # Fill a random template
        tmpl = random.choice(self.templates)
        prompt = tmpl.format(
            opener=opener,
            article=article,
            head=head,
            action_phrase=action_phrase,
            env_clause=env_clause,
            weather_clause=weather_clause,
            desc_clause=desc_clause,
            style=style
        )

        return prompt.strip(" ,!?.")

class APIPromptGenerator:
    def __init__(self, server_url: str, random_generator, endpoint: str = "/flux/generate_text"):
        self.base_url = server_url.rstrip('/')
        self.endpoint = endpoint
        self.random_generator = random_generator
        self.last_prompts: list[str] = []  # keep track of the last five generated

    def _build_instruction(self, theme: str) -> str:
        # Include the last five generated prompts, if any
        prev_section = ""
        if self.last_prompts:
            lines = "\n".join(f"{i+1}. {p}" for i, p in enumerate(self.last_prompts))
            prev_section = f"Previous prompts (avoid repeating these):\n{lines}\n\n"

        # Generate ten fresh random examples
        examples = [self.random_generator.GetNextRandomPrompt() for _ in range(10)]
        examples_text = "\n".join(f"{i+1}. {ex}" for i, ex in enumerate(examples))

        # Uniqueness clause
        uniqueness = (
            "Ensure that the prompt you generate follows the user request exactly but "
            "is unique—do not repeat or closely mimic any of the above examples or previous prompts."
        )

        return (
            f"{prev_section}"
            f"Random examples:\n{examples_text}\n\n"
            f"You are an AI image generation prompt creator. Based on the examples above "
            f"and the user request \"{theme}\", generate a single high-quality image prompt. "
            f"{uniqueness}\n\n"
            "Respond with valid JSON **only**, using exactly this format:\n"
            "{\n"
            "  \"id\": \"<UUID>\",\n"
            "  \"image_prompt\": \"<your generated prompt here>\"\n"
            "}"
        )

    def generate(
        self,
        theme: str,
        max_new_tokens: int = 256,
        temperature: float = 1.5
    ) -> str:
        request_id = str(uuid.uuid4())
        url = f"{self.base_url}{self.endpoint}"
        instruction = self._build_instruction(theme).replace("<UUID>", request_id)
        payload = {
            "text": instruction,
            "max_new_tokens": max_new_tokens,
            "temperature": temperature,
        }

        # Verbose logging
        print(f"[DEBUG] REQUEST ID: {request_id}")
        print(f"[DEBUG] POST {url}")
        print(f"[DEBUG] Instruction:\n{instruction}")
        print(f"[DEBUG] Payload: {payload}")

        resp = requests.post(url, json=payload)
        print(f"[DEBUG] Response status: {resp.status_code}")
        print(f"[DEBUG] Response body:\n{resp.text}")
        resp.raise_for_status()

        outer = resp.json()
        try:
            inner = json.loads(outer["text"])
        except Exception as e:
            raise ValueError(f"Failed to parse inner JSON: {e}\nInner text was: {outer.get('text')}")

        resp_id = inner.get("id")
        prompt = inner.get("image_prompt")

        print(f"[DEBUG] RESPONSE ID: {resp_id}")
        print(f"[DEBUG] EXTRACTED PROMPT: {prompt}")

        # Update buffer of last prompts
        self.last_prompts.append(prompt)
        if len(self.last_prompts) > 5:
            self.last_prompts.pop(0)

        return prompt
    
class ImageManager:
    def __init__(self, out_dir: str, meta_filename: str = 'metadata.json'):
        self.out_dir = out_dir
        os.makedirs(self.out_dir, exist_ok=True)
        self.meta_path = os.path.join(self.out_dir, meta_filename)
        if os.path.exists(self.meta_path):
            with open(self.meta_path, 'r', encoding='utf-8') as f:
                self.meta = json.load(f)
        else:
            self.meta = []
        self._cache: dict[int, tuple[int, tuple[int, int]]] = {}

    def count(self) -> int:
        return len(self.meta)

    def get_prompt(self, index: int) -> str:
        return self.meta[index]['prompt']

    def get_texture(self, index: int) -> tuple[int, tuple[int, int]]:
        if index < 0 or index >= len(self.meta):
            raise IndexError(f"Index {index} out of range (0-{len(self.meta)-1})")
        if index not in self._cache:
            entry = self.meta[index]
            img_path = os.path.join(self.out_dir, entry['filename'])
            img = Image.open(img_path).convert("RGBA")
            w, h = img.width, img.height
            data = img.tobytes()
            tex_id = GL.glGenTextures(1)
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex_id)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MIN_FILTER, GL.GL_LINEAR)
            GL.glTexParameteri(GL.GL_TEXTURE_2D, GL.GL_TEXTURE_MAG_FILTER, GL.GL_LINEAR)
            GL.glPixelStorei(GL.GL_UNPACK_ALIGNMENT, 1)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGBA, w, h, 0, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE, data)
            self._cache[index] = (tex_id, (w, h))
        return self._cache[index]

    def preload(self, start_index: int = 0, count: int = 10) -> None:
        end = min(start_index + count, len(self.meta))
        for idx in range(start_index, end):
            try:
                self.get_texture(idx)
            except Exception:
                pass

    def save(self, pil_img: Image.Image, prompt: str) -> int:
        idx = len(self.meta) + 1
        filename = f"img_{idx:04d}.png"
        img_path = os.path.join(self.out_dir, filename)
        pil_img.save(img_path)
        self.meta.append({'filename': filename, 'prompt': prompt})
        with open(self.meta_path, 'w', encoding='utf-8') as f:
            json.dump(self.meta, f, indent=2, ensure_ascii=False)
        return len(self.meta) - 1

    def clear_cache(self):
        for tex_id, _ in self._cache.values():
            try:
                GL.glDeleteTextures([tex_id])
            except Exception:
                pass
        self._cache.clear()

# ---------- Flux Client Wrappers ----------
SERVER_URL = "http://127.0.0.1:8000"
prompt_generator = RandomPromptGenerator('src/bridge_python_sdk/assets/prompts_landscapes.json')
api_prompt_gen = APIPromptGenerator(SERVER_URL, prompt_generator)

def fetch_image(endpoint: str, payload: dict) -> bytes:
    url = f"{SERVER_URL.rstrip('/')}{endpoint}"
    resp = requests.post(url, json=payload)
    resp.raise_for_status()
    return base64.b64decode(resp.json()["image"])

def generate_rgbd(prompt: str, steps: int, scale: float, h: int, w: int, max_len: int) -> bytes:
    return fetch_image("/flux/generate_rgbd", {
        "prompt": prompt,
        "num_inference_steps": steps,
        "guidance_scale": scale,
        "height": h,
        "width": w,
        "max_sequence_length": max_len,
    })

# ---------- GUI & Rendering ----------
def run_gui() -> None:
    if not glfw.init():
        print("Could not initialize GLFW", file=sys.stderr)
        sys.exit(1)

    window = glfw.create_window(1600, 900, "Flux GUI", None, None)
    glfw.make_context_current(window)
    imgui.create_context()
    impl = GlfwRenderer(window)

    bridge = BridgeAPI()
    if not bridge.initialize("FluxGUI"):
        print("BridgeAPI init failed", file=sys.stderr)
        return

    br_wnd = bridge.instance_window_gl(-1)
    aspect_ratio, qw, qh, cols, rows = bridge.get_default_quilt_settings(br_wnd)
    cols, rows = 10, 10
    aspect = float(aspect_ratio)

    script_dir = os.path.dirname(__file__)
    out_dir = os.path.join(script_dir, 'out')
    image_mgr = ImageManager(out_dir)

    current_index = 0
    prompt = "A dog playing fetch"
    theme = ""
    focus, depthiness = 0.0, 1.5
    steps, scale = 4, 1.0
    height, width, max_len = 768, 1360, 256

    auto_looping = False
    themed_looping = False
    loop_thread = themed_thread = None
    loop_stop_event = themed_stop_event = threading.Event()
    result_queue = queue.Queue()

    def enqueue(p: str, img: Image.Image):
        result_queue.put((p, img))

    def loop_random():
        while not loop_stop_event.is_set():
            p = prompt_generator.GetNextRandomPrompt()
            try:
                data = generate_rgbd(p, steps, scale, height, width, max_len)
                combo = Image.open(BytesIO(data)).convert("RGBA")
                enqueue(p, combo)
            except Exception as e:
                print(f"Error in random loop: {e}", file=sys.stderr)
            time.sleep(0.1)

    def loop_themed():
        while not themed_stop_event.is_set():
            try:
                p = api_prompt_gen.generate(theme)
                data = generate_rgbd(p, steps, scale, height, width, max_len)
                combo = Image.open(BytesIO(data)).convert("RGBA")
                enqueue(p, combo)
            except Exception as e:
                print(f"Error in themed loop: {e}", file=sys.stderr)
            time.sleep(0.1)

    while not glfw.window_should_close(window):
        glfw.poll_events()
        impl.process_inputs()
        imgui.new_frame()
        io = imgui.get_io()

        # Process queue
        while not result_queue.empty():
            p, img = result_queue.get()
            new_idx = image_mgr.save(img, p)
            current_index = new_idx

        imgui.set_next_window_position(0, 0)
        imgui.set_next_window_size(io.display_size[0], io.display_size[1])
        imgui.begin("Flux Fullscreen", flags=imgui.WINDOW_NO_TITLE_BAR | imgui.WINDOW_NO_RESIZE | imgui.WINDOW_NO_MOVE)

        # Controls
        imgui.begin_child("Controls", width=io.display_size[0]*0.3, height=0, border=True)
        imgui.text("Flux Controls")
        _, prompt = imgui.input_text("Prompt", prompt, 256)
        _, theme = imgui.input_text("Theme", theme, 256)
        if imgui.button("Random Prompt"):
            prompt = prompt_generator.GetNextRandomPrompt()
        imgui.same_line()
        if imgui.button("Generate Prompt"):
            try:
                prompt = api_prompt_gen.generate(theme)
            except Exception as e:
                print(f"Error generating prompt: {e}", file=sys.stderr)

        imgui.separator()
        if not themed_looping:
            if imgui.button("Start Themed RGBD Loop"):
                themed_looping = True
                themed_stop_event.clear()
                themed_thread = threading.Thread(target=loop_themed, daemon=True)
                themed_thread.start()
        else:
            if imgui.button("Stop Themed RGBD Loop"):
                themed_looping = False
                themed_stop_event.set()
                themed_thread.join()

        imgui.separator()
        if not auto_looping:
            if imgui.button("Start RGBD Loop"):
                auto_looping = True
                loop_stop_event.clear()
                loop_thread = threading.Thread(target=loop_random, daemon=True)
                loop_thread.start()
        else:
            if imgui.button("Stop RGBD Loop"):
                auto_looping = False
                loop_stop_event.set()
                loop_thread.join()

        imgui.separator()
        _, focus = imgui.slider_float("Focus", focus, -1.0, 1.0)
        _, depthiness = imgui.slider_float("Depthiness", depthiness, 0.0, 3.0)
        if imgui.button("Generate RGBD"):
            try:
                data = generate_rgbd(prompt, steps, scale, height, width, max_len)
                combo = Image.open(BytesIO(data)).convert("RGBA")
                enqueue(prompt, combo)
            except Exception as e:
                print(f"Error generating RGBD: {e}", file=sys.stderr)
        imgui.same_line()
        imgui.end_child()

        # Preview
        imgui.same_line()
        imgui.begin_child("RGBD Panel", width=0, height=0, border=True)
        imgui.text("Generated RGBD Output")
        if image_mgr.count() > 0:
            tex_id, (w, h) = image_mgr.get_texture(current_index)
            max_w = io.display_size[0]*0.65 - 20
            disp_w = min(w, max_w)
            disp_h = int(h * disp_w / w)
            imgui.image(tex_id, disp_w, disp_h)
            imgui.separator()
            if imgui.button("Prev") and current_index > 0:
                current_index -= 1
            imgui.same_line()
            if imgui.button("Next") and current_index < image_mgr.count() - 1:
                current_index += 1
            imgui.separator()
            imgui.text_wrapped(image_mgr.get_prompt(current_index))
        else:
            imgui.text("No saved images yet.")
        imgui.end_child()

        imgui.end()

        # Render to Looking Glass
        if image_mgr.count() > 0:
            tex_id, (w, h) = image_mgr.get_texture(current_index)
            zoom = 1.0
            depth_loc = 2

            bridge.draw_interop_rgbd_texture_gl(
                br_wnd,
                tex_id,
                PixelFormats.RGBA,
                w,
                h,
                qw,
                qh,
                cols,
                rows,
                aspect,
                focus * depthiness,  # normalized focus
                depthiness,
                zoom,
                depth_loc
            )

        imgui.render()
        impl.render(imgui.get_draw_data())
        glfw.swap_buffers(window)

    # Cleanup
    loop_stop_event.set()
    themed_stop_event.set()
    if loop_thread: loop_thread.join()
    if themed_thread: themed_thread.join()
    image_mgr.clear_cache()
    impl.shutdown()
    glfw.terminate()

if __name__ == "__main__":
    run_gui()
