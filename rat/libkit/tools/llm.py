from openai import OpenAI
import time

model_configs = {
    "deepseek-chat": {
        "base_url": "https://api.deepseek.com",
        "key": "sk-<your_deepseek_api_key>",
    },
    "glm-4-flash": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "key": "<your_glm_api_key>",
    },
}


class LLMChat:
    def __init__(self, model: str):
        self.model = model
        config = model_configs.get(self.model)
        self.client = OpenAI(base_url=config["base_url"], api_key=config["key"])

    def chat(self, messages, temperature=0.0, n=1, max_tokens=1024):
        max_retry = 5
        count = 0
        while count < max_retry:
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=temperature,
                    n=n,
                    max_tokens=max_tokens,
                )
                return response.choices[0].message.content, response.usage
            except Exception as e:
                print(f"Error: {e}")
                count += 1
                time.sleep(3)
        return None, None
