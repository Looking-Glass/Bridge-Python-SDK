import os
import requests
import json
import random
import uuid

from PIL import Image
from OpenGL import GL

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
        self.weather_preps = ["under", "with", "amidst"]

        # Shorter, focused templates
        self.templates = [
            "{article} {head} {action_phrase} {env_clause}.",
            "{article} {head} {action_phrase} {weather_clause}.",
            "{opener}! {head} {action_phrase}.",
        ]

    def GetNextRandomPrompt(self, max_words=10):
        # Pick 1-2 adjectives and maybe 1 color
        num_adjs = random.randint(1, 2)
        adjs = random.sample(self.adjectives, k=num_adjs)
        if random.random() < 0.3:
            adjs.insert(random.randrange(len(adjs) + 1), random.choice(self.colors))
        head = " ".join(adjs + [random.choice(self.subjects)])
        article = "An" if head[0].lower() in "aeiou" else "A"

        # Action (no second subject to reduce length)
        action = random.choice(self.actions)
        action_phrase = random.choice([action, action + "ing"])

        # Environment or weather clause
        env_clause = f"{random.choice(self.env_preps)} {random.choice(self.environments)}"
        weather_clause = f"{random.choice(self.weather_preps)} {random.choice(self.weather)}"

        # Single descriptor and style
        desc = random.choice(self.descriptors)
        desc_clause = random.choice([desc, f"highlighting {desc}"])
        style_key = random.choice(self.styles)
        style = random.choice([style_key, f"in {style_key} style"])

        # Optional opener
        opener = random.choice(self.openers) if random.random() < 0.4 else ""

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
        ).strip(" ,!?.") + f" {style} {desc_clause}"

        # Trim to max_words
        words = prompt.split()
        trimmed = " ".join(words[:max_words])
        return trimmed


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
        examples = [self.random_generator.GetNextRandomPrompt() for _ in range(4)]
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
            f"and the user request \"{theme}\", generate a single high-quality image prompt. The prompt should be as simple and short as reasonable. "
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