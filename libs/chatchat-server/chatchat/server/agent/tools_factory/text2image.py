import base64
from datetime import datetime
import os
import uuid
from typing import List, Literal

import openai
from PIL import Image

from chatchat.settings import Settings
from chatchat.server.pydantic_v1 import Field
from chatchat.server.utils import MsgType, get_tool_config, get_model_info

from .tools_registry import regist_tool

from langchain_chatchat.agent_toolkits.all_tools.tool import (
    BaseToolOutput,
)

SUPPORTED_SIZES = (
    "1024x1024",
    "768x1344",
    "864x1152",
    "1344x768",
    "1152x864",
    "1440x720",
    "720x1440",
)
DEFAULT_SIZE = "1024x1024"


def _resolve_size(requested, configured) -> str:
    """配置里的 size 历史上是死配置（工具从不读它），现在作为默认值。

    尺寸不在支持列表里时退回默认值，而不是让 Literal 校验把整个工具调用
    变成一句模型和用户都看不懂的 validation error。
    配置里的写法是 `256*256`（星号），这里一并归一化。
    """
    for candidate in (requested, configured):
        text = str(candidate or "").strip().lower().replace("*", "x")
        if text in SUPPORTED_SIZES:
            return text
    return DEFAULT_SIZE


@regist_tool(title="""
#文本生成图片工具
##描述
则根据用户的描述生成图片。
##请求参数
参数名	类型	必填	描述
prompt	String	是	所需图像的文本描述
size	String	否	图片尺寸，可选值：1024x1024,768x1344,864x1152,1344x768,1152x864,1440x720,720x1440，默认是1024x1024。
""", return_direct=True)
def text2images(
    prompt: str = Field(description="用户的描述"),
    n: int = Field(1, description="需生成图片的数量"),
    size: str = Field(
        DEFAULT_SIZE,
        description="图片尺寸，可选值：1024x1024,768x1344,864x1152,1344x768,"
                    "1152x864,1440x720,720x1440，默认 1024x1024",
    ),
):
    """根据用户的描述生成图片"""

    tool_config = get_tool_config("text2images")
    model_name = tool_config.get("model")
    model_config = get_model_info(model_name) if model_name else None
    if not model_config:
        # 以前是一句裸 assert：AssertionError 穿透到接口层只剩 "failed to call tool"
        return BaseToolOutput({
            "error": f"未配置文生图模型：tool_settings.yaml 的 text2images.model 是"
                     f"「{model_name}」，它不在任何平台的 text2image_models 里"
                     "（model_settings.yaml）"
        })

    size = _resolve_size(size, tool_config.get("size"))

    try:
        client = openai.Client(
            base_url=model_config["api_base_url"],
            api_key=model_config["api_key"],
            timeout=600,
        )
        resp = client.images.generate(
            prompt=prompt,
            n=n,
            size=size,
            response_format="b64_json",
            model=model_config["model_name"],
        )
    except Exception as exc:
        # 密钥无效、平台不通、余额不足都只在这里能看出来，必须带出来
        return BaseToolOutput({
            "error": f"调用文生图模型「{model_config.get('model_name')}」失败："
                     f"{type(exc).__name__}: {exc}"
                     f"（api_base_url {model_config.get('api_base_url')}）"
        })

    images = []
    for x in getattr(resp, "data", None) or []:
        if x.b64_json is not None:
            uid = uuid.uuid4().hex
            today = datetime.now().strftime("%Y-%m-%d")
            path = os.path.join(Settings.basic_settings.MEDIA_PATH, "image", today)
            os.makedirs(path, exist_ok=True)
            filename = f"image/{today}/{uid}.png"
            with open(os.path.join(Settings.basic_settings.MEDIA_PATH, filename), "wb") as fp:
                fp.write(base64.b64decode(x.b64_json))
            images.append(filename)
        else:
            images.append(x.url)
    if not images:
        return BaseToolOutput({
            "error": f"文生图模型「{model_config.get('model_name')}」没有返回图片"
        })
    return BaseToolOutput(
        {"message_type": MsgType.IMAGE, "images": images}, format="json"
    )


if __name__ == "__main__":
    import sys
    from io import BytesIO
    from pathlib import Path

    from matplotlib import pyplot as plt

    sys.path.append(str(Path(__file__).parent.parent.parent.parent))

    prompt = "draw a house with trees and river"
    prompt = "画一个带树、草、河流的山中小屋"
    params = text2images.args_schema.parse_obj({"prompt": prompt}).dict()
    print(params)
    image = text2images.invoke(params)[0]
    buffer = BytesIO(base64.b64decode(image))
    image = Image.open(buffer)
    plt.imshow(image)
    plt.show()
