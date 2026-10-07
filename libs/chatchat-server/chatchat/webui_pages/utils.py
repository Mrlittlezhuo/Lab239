# 该文件封装了对api.py的请求，可以被不同的webui使用
# 通过ApiRequest和AsyncApiRequest支持同步/异步调用

import base64
import contextlib
import json
import logging
import os
from io import BytesIO
from pathlib import Path
from typing import *

import httpx

from chatchat.settings import Settings
from chatchat.server.utils import api_address, get_httpx_client, set_httpx_config, get_default_embedding
from chatchat.utils import build_logger


logger = build_logger()

set_httpx_config()


class AuthenticationError(Exception):
    """API 返回 401（未登录/Token 过期/用户失效）时抛出，WebUI 据此清理本地登录状态。"""


class MemoryApiError(Exception):
    """记忆接口返回非 2xx（非认证）状态时抛出，如 404（他人记忆）/400（参数错误）。"""


class ApiRequest:
    """
    api.py调用的封装（同步模式）,简化api调用方式
    """

    def __init__(
        self,
        base_url: str = api_address(),
        timeout: float = Settings.basic_settings.HTTPX_DEFAULT_TIMEOUT,
        token: Optional[str] = None,
    ):
        self.base_url = base_url
        self.timeout = timeout
        self._use_async = False
        self._client = None
        # 登录后的 Bearer Token；None 表示未登录，不附加 Authorization 头
        self.token: Optional[str] = token

    def _auth_headers(self, provided: Optional[Dict] = None) -> Dict:
        """合并调用方提供的 headers 与登录 Token。

        - 未登录（无 Token）时不附加 Authorization。
        - 调用方已显式提供 Authorization 时，不覆盖（caller-supplied 优先）。
        - 其余调用方 headers 一律保留。
        """
        headers = dict(provided) if provided else {}
        if self.token and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @property
    def client(self):
        if self._client is None or self._client.is_closed:
            self._client = get_httpx_client(
                base_url=self.base_url, use_async=self._use_async, timeout=self.timeout
            )
        return self._client

    def get(
        self,
        url: str,
        params: Union[Dict, List[Tuple], bytes] = None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        headers = self._auth_headers(kwargs.pop("headers", None))
        while retry > 0:
            try:
                if stream:
                    return self.client.stream("GET", url, params=params, headers=headers, **kwargs)
                else:
                    return self.client.get(url, params=params, headers=headers, **kwargs)
            except Exception as e:
                msg = f"error when get {url}: {e}"
                logger.error(f"{e.__class__.__name__}: {msg}")
                retry -= 1

    def post(
        self,
        url: str,
        data: Dict = None,
        json: Dict = None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        headers = self._auth_headers(kwargs.pop("headers", None))
        while retry > 0:
            try:
                # print(kwargs)
                if stream:
                    return self.client.stream(
                        "POST", url, data=data, json=json, headers=headers, **kwargs
                    )
                else:
                    return self.client.post(url, data=data, json=json, headers=headers, **kwargs)
            except Exception as e:
                msg = f"error when post {url}: {e}"
                logger.error(f"{e.__class__.__name__}: {msg}")
                retry -= 1

    def delete(
        self,
        url: str,
        data: Dict = None,
        json: Dict = None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        headers = self._auth_headers(kwargs.pop("headers", None))
        while retry > 0:
            try:
                if stream:
                    return self.client.stream(
                        "DELETE", url, data=data, json=json, headers=headers, **kwargs
                    )
                else:
                    return self.client.delete(url, data=data, json=json, headers=headers, **kwargs)
            except Exception as e:
                msg = f"error when delete {url}: {e}"
                logger.error(f"{e.__class__.__name__}: {msg}")
                retry -= 1

    def put(
        self,
        url: str,
        data: Dict = None,
        json: Dict = None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        headers = self._auth_headers(kwargs.pop("headers", None))
        while retry > 0:
            try:
                if stream:
                    return self.client.stream(
                        "PUT", url, data=data, json=json, headers=headers, **kwargs
                    )
                else:
                    return self.client.put(url, data=data, json=json, headers=headers, **kwargs)
            except Exception as e:
                msg = f"error when put {url}: {e}"
                logger.error(f"{e.__class__.__name__}: {msg}")
                retry -= 1

    def patch(
        self,
        url: str,
        data: Dict = None,
        json: Dict = None,
        retry: int = 3,
        stream: bool = False,
        **kwargs: Any,
    ) -> Union[httpx.Response, Iterator[httpx.Response], None]:
        headers = self._auth_headers(kwargs.pop("headers", None))
        while retry > 0:
            try:
                if stream:
                    return self.client.stream(
                        "PATCH", url, data=data, json=json, headers=headers, **kwargs
                    )
                else:
                    return self.client.patch(url, data=data, json=json, headers=headers, **kwargs)
            except Exception as e:
                msg = f"error when patch {url}: {e}"
                logger.error(f"{e.__class__.__name__}: {msg}")
                retry -= 1

    def _httpx_stream2generator(
        self,
        response: contextlib._GeneratorContextManager,
        as_json: bool = False,
    ):
        """
        将httpx.stream返回的GeneratorContextManager转化为普通生成器
        """

        async def ret_async(response, as_json):
            try:
                async with response as r:
                    chunk_cache = ""
                    async for chunk in r.aiter_text(None):
                        if not chunk:  # fastchat api yield empty bytes on start and end
                            continue
                        if as_json:
                            try:
                                if chunk.startswith("data: "):
                                    data = json.loads(chunk_cache + chunk[6:-2])
                                elif chunk.startswith(":"):  # skip sse comment line
                                    continue
                                else:
                                    data = json.loads(chunk_cache + chunk)

                                chunk_cache = ""
                                yield data
                            except Exception as e:
                                msg = f"接口返回json错误： ‘{chunk}’。错误信息是：{e}。"
                                logger.error(f"{e.__class__.__name__}: {msg}")

                                if chunk.startswith("data: "):
                                    chunk_cache += chunk[6:-2]
                                elif chunk.startswith(":"):  # skip sse comment line
                                    continue
                                else:
                                    chunk_cache += chunk
                                continue
                        else:
                            # print(chunk, end="", flush=True)
                            yield chunk
            except httpx.ConnectError as e:
                msg = f"无法连接API服务器，请确认 ‘api.py’ 已正常启动。({e})"
                logger.error(msg)
                yield {"code": 500, "msg": msg}
            except httpx.ReadTimeout as e:
                msg = f"API通信超时，请确认已启动FastChat与API服务（详见Wiki '5. 启动 API 服务或 Web UI'）。（{e}）"
                logger.error(msg)
                yield {"code": 500, "msg": msg}
            except Exception as e:
                msg = f"API通信遇到错误：{e}"
                logger.error(f"{e.__class__.__name__}: {msg}")
                yield {"code": 500, "msg": msg}

        def ret_sync(response, as_json):
            try:
                with response as r:
                    chunk_cache = ""
                    for chunk in r.iter_text(None):
                        if not chunk:  # fastchat api yield empty bytes on start and end
                            continue
                        if as_json:
                            try:
                                if chunk.startswith("data: "):
                                    data = json.loads(chunk_cache + chunk[6:-2])
                                elif chunk.startswith(":"):  # skip sse comment line
                                    continue
                                else:
                                    data = json.loads(chunk_cache + chunk)

                                chunk_cache = ""
                                yield data
                            except Exception as e:
                                msg = f"接口返回json错误： ‘{chunk}’。错误信息是：{e}。"
                                logger.error(f"{e.__class__.__name__}: {msg}")

                                if chunk.startswith("data: "):
                                    chunk_cache += chunk[6:-2]
                                elif chunk.startswith(":"):  # skip sse comment line
                                    continue
                                else:
                                    chunk_cache += chunk
                                continue
                        else:
                            # print(chunk, end="", flush=True)
                            yield chunk
            except httpx.ConnectError as e:
                msg = f"无法连接API服务器，请确认 ‘api.py’ 已正常启动。({e})"
                logger.error(msg)
                yield {"code": 500, "msg": msg}
            except httpx.ReadTimeout as e:
                msg = f"API通信超时，请确认已启动FastChat与API服务（详见Wiki '5. 启动 API 服务或 Web UI'）。（{e}）"
                logger.error(msg)
                yield {"code": 500, "msg": msg}
            except Exception as e:
                msg = f"API通信遇到错误：{e}"
                logger.error(f"{e.__class__.__name__}: {msg}")
                yield {"code": 500, "msg": msg}

        if self._use_async:
            return ret_async(response, as_json)
        else:
            return ret_sync(response, as_json)

    def _get_response_value(
        self,
        response: httpx.Response,
        as_json: bool = False,
        value_func: Callable = None,
    ):
        """
        转换同步或异步请求返回的响应
        `as_json`: 返回json
        `value_func`: 用户可以自定义返回值，该函数接受response或json
        """

        def to_json(r):
            try:
                return r.json()
            except Exception as e:
                msg = "API未能返回正确的JSON。" + str(e)
                logger.error(f"{e.__class__.__name__}: {msg}")
                return {"code": 500, "msg": msg, "data": None}

        if value_func is None:
            value_func = lambda r: r

        async def ret_async(response):
            if as_json:
                return value_func(to_json(await response))
            else:
                return value_func(await response)

        if self._use_async:
            return ret_async(response)
        else:
            if as_json:
                return value_func(to_json(response))
            else:
                return value_func(response)

    # 服务器信息
    def get_server_configs(self, **kwargs) -> Dict:
        response = self.post("/server/configs", **kwargs)
        return self._get_response_value(response, as_json=True)

    def get_prompt_template(
        self,
        type: str = "llm_chat",
        name: str = "default",
        **kwargs,
    ) -> str:
        data = {
            "type": type,
            "name": name,
        }
        response = self.post("/server/get_prompt_template", json=data, **kwargs)
        return self._get_response_value(response, value_func=lambda r: r.text)

    # 对话相关操作
    def chat_chat(
        self,
        query: str,
        metadata: dict,
        conversation_id: str = None,
        history_len: int = -1,
        history: List[Dict] = [],
        stream: bool = True,
        chat_model_config: Dict = None,
        tool_config: Dict = None,
        **kwargs,
    ):
        """
        对应api.py/chat/chat接口
        """
        data = {
            "query": query,
            "metadata": metadata,
            "conversation_id": conversation_id,
            "history_len": history_len,
            "history": history,
            "stream": stream,
            "chat_model_config": chat_model_config,
            "tool_config": tool_config,
        }

        # print(f"received input message:")
        # pprint(data)

        response = self.post("/chat/chat", json=data, stream=True, **kwargs)
        return self._httpx_stream2generator(response, as_json=True)

    def upload_temp_docs(
        self,
        files: List[Union[str, Path, bytes]],
        knowledge_id: str = None,
        chunk_size=Settings.kb_settings.CHUNK_SIZE,
        chunk_overlap=Settings.kb_settings.OVERLAP_SIZE,
        zh_title_enhance=Settings.kb_settings.ZH_TITLE_ENHANCE,
    ):
        """
        对应api.py/knowledge_base/upload_temp_docs接口
        """

        def convert_file(file, filename=None):
            if isinstance(file, bytes):  # raw bytes
                file = BytesIO(file)
            elif hasattr(file, "read"):  # a file io like object
                filename = filename or file.name
            else:  # a local path
                file = Path(file).absolute().open("rb")
                filename = filename or os.path.split(file.name)[-1]
            return filename, file

        files = [convert_file(file) for file in files]
        data = {
            "knowledge_id": knowledge_id,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "zh_title_enhance": zh_title_enhance,
        }

        response = self.post(
            "/knowledge_base/upload_temp_docs",
            data=data,
            files=[("files", (filename, file)) for filename, file in files],
        )
        return self._get_response_value(response, as_json=True)

    def file_chat(
        self,
        query: str,
        knowledge_id: str,
        top_k: int = Settings.kb_settings.VECTOR_SEARCH_TOP_K,
        score_threshold: float = Settings.kb_settings.SCORE_THRESHOLD,
        history: List[Dict] = [],
        stream: bool = True,
        model: str = None,
        temperature: float = 0.9,
        max_tokens: int = None,
        prompt_name: str = "default",
    ):
        """
        对应api.py/chat/file_chat接口
        """
        data = {
            "query": query,
            "knowledge_id": knowledge_id,
            "top_k": top_k,
            "score_threshold": score_threshold,
            "history": history,
            "stream": stream,
            "model_name": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "prompt_name": prompt_name,
        }

        response = self.post(
            "/chat/file_chat",
            json=data,
            stream=True,
        )
        return self._httpx_stream2generator(response, as_json=True)

    # 认证相关操作
    # 注意：/auth/* 与 /conversations/* 接口直接返回 JSON（无 {"code","data"} 包装）。
    # 401 统一抛出 AuthenticationError，由 WebUI 触发本地登录状态清理。

    def _raise_for_auth(self, response) -> None:
        # 401：未登录 / Token 过期 / 用户已失效
        # 403：必须改密等「未完全授权」状态
        # 两者都要求 WebUI 清理本地登录态并回到登录页。
        if response is not None and response.status_code in (401, 403):
            raise AuthenticationError("认证失败：未登录、Token 过期、用户已失效或未改密")

    def login(self, username: str, password: str, **kwargs):
        """对应 POST /auth/login，返回 {"token","token_type","expires_in","user"}"""
        data = {"username": username, "password": password}
        response = self.post("/auth/login", json=data, retry=1, **kwargs)
        if response is None:
            raise AuthenticationError("无法连接API服务器，登录失败")
        self._raise_for_auth(response)
        # 仅 2xx 才视为登录成功；其它状态码（如 404/500）一律失败，
        # 绝不把没有 token 的载荷当成功返回。
        if response.status_code >= 400:
            raise AuthenticationError("登录失败：服务器返回异常状态")
        return self._get_response_value(response, as_json=True)

    def me(self, **kwargs):
        """对应 GET /auth/me，校验当前 Token，返回用户信息；401 抛出 AuthenticationError"""
        response = self.get("/auth/me", retry=1, **kwargs)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        if response.status_code >= 400:
            raise AuthenticationError("无法校验当前用户（登录可能已失效）")
        return self._get_response_value(response, as_json=True)

    def logout(self, **kwargs):
        """对应 POST /auth/logout；无论请求成败，调用方都应清理本地登录状态"""
        response = self.post("/auth/logout", retry=1, **kwargs)
        return self._get_response_value(response, as_json=True)

    def change_password(self, old_password: str, new_password: str, **kwargs):
        """对应 POST /auth/change-password（请求体字段为 old_password/password）"""
        data = {"old_password": old_password, "password": new_password}
        response = self.post("/auth/change-password", json=data, retry=1, **kwargs)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        if response.status_code >= 400:
            raise AuthenticationError("修改密码失败：服务器返回异常状态")
        return self._get_response_value(response, as_json=True)

    # 会话（conversation）相关操作 —— 后端为会话事实来源

    def list_conversations(self, **kwargs) -> List[Dict]:
        """对应 GET /conversations，返回当前用户会话列表（每项含 id/name，update_time DESC）"""
        response = self.get("/conversations", retry=1, **kwargs)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        return self._get_response_value(response, as_json=True)

    def create_conversation(
        self, name: str = "新建对话", chat_type: str = "agent_chat", **kwargs
    ) -> Dict:
        """对应 POST /conversations，返回新会话（含 id）"""
        data = {"name": name, "chat_type": chat_type}
        response = self.post("/conversations", json=data, retry=1, **kwargs)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        return self._get_response_value(response, as_json=True)

    def get_conversation(self, conversation_id: str, **kwargs) -> Dict:
        """对应 GET /conversations/{id}"""
        response = self.get(f"/conversations/{conversation_id}", retry=1, **kwargs)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        return self._get_response_value(response, as_json=True)

    def rename_conversation(self, conversation_id: str, name: str, **kwargs) -> Dict:
        """对应 PATCH /conversations/{id}"""
        data = {"name": name}
        response = self.patch(f"/conversations/{conversation_id}", json=data, retry=1, **kwargs)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        return self._get_response_value(response, as_json=True)

    def delete_conversation(self, conversation_id: str, **kwargs) -> Dict:
        """对应 DELETE /conversations/{id}"""
        response = self.delete(f"/conversations/{conversation_id}", retry=1, **kwargs)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        return self._get_response_value(response, as_json=True)

    def conversation_messages(self, conversation_id: str, **kwargs) -> List[Dict]:
        """对应 GET /conversations/{id}/messages，按 create_time ASC 返回历史消息"""
        response = self.get(f"/conversations/{conversation_id}/messages", retry=1, **kwargs)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        data = self._get_response_value(response, as_json=True)
        if isinstance(data, dict):
            return data.get("messages", [])
        return data if isinstance(data, list) else []

    # 我的记忆（memories）相关操作 —— 后端只返回/操作当前用户自己的记忆
    # 接口直接返回 JSON（无 {"code","data"} 包装）；401/403 抛出 AuthenticationError

    def _mem_request(self, method, url, **kw) -> httpx.Response:
        fn = getattr(self, method)
        response = fn(url, retry=1, **kw)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        if response.status_code >= 400:
            raise MemoryApiError(f"记忆操作失败：服务器返回 {response.status_code}")
        return response

    def list_memories(self) -> List[Dict]:
        """对应 GET /memories，返回当前用户记忆列表（update_time DESC）。"""
        r = self._mem_request("get", "/memories")
        return self._get_response_value(r, as_json=True)

    def create_memory(self, type: str, content: str, importance: int = 1,
                      source: str = "user") -> Dict:
        """对应 POST /memories，创建当前用户记忆。"""
        data = {"type": type, "content": content, "importance": importance, "source": source}
        r = self._mem_request("post", "/memories", json=data)
        return self._get_response_value(r, as_json=True)

    def update_memory(self, memory_id: str, **fields) -> Dict:
        """对应 PATCH /memories/{id}，更新当前用户记忆（仅传非 None 字段）。"""
        data = {k: v for k, v in fields.items() if v is not None}
        r = self._mem_request("patch", f"/memories/{memory_id}", json=data)
        return self._get_response_value(r, as_json=True)

    def delete_memory(self, memory_id: str) -> Dict:
        """对应 DELETE /memories/{id}，删除当前用户记忆。"""
        r = self._mem_request("delete", f"/memories/{memory_id}")
        return self._get_response_value(r, as_json=True)

    def get_auto_memory(self) -> bool:
        """对应 GET /memories/auto，返回当前用户自动记忆开关。"""
        r = self._mem_request("get", "/memories/auto")
        return bool(self._get_response_value(r, as_json=True).get("auto_memory"))

    def set_auto_memory(self, enabled: bool) -> Dict:
        """对应 PATCH /memories/auto?enabled=...，设置当前用户自动记忆开关。"""
        r = self._mem_request("patch", f"/memories/auto?enabled={'true' if enabled else 'false'}")
        return self._get_response_value(r, as_json=True)

    # 知识库相关操作

    def list_knowledge_bases(
        self,
    ):
        """
        对应api.py/knowledge_base/list_knowledge_bases接口
        """
        response = self.get("/knowledge_base/list_knowledge_bases")
        return self._get_response_value(
            response, as_json=True, value_func=lambda r: r.get("data", [])
        )

    def create_knowledge_base(
        self,
        knowledge_base_name: str,
        vector_store_type: str = Settings.kb_settings.DEFAULT_VS_TYPE,
        embed_model: str = get_default_embedding(),
    ):
        """
        对应api.py/knowledge_base/create_knowledge_base接口
        """
        data = {
            "knowledge_base_name": knowledge_base_name,
            "vector_store_type": vector_store_type,
            "embed_model": embed_model,
        }

        response = self.post(
            "/knowledge_base/create_knowledge_base",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def delete_knowledge_base(
        self,
        knowledge_base_name: str,
    ):
        """
        对应api.py/knowledge_base/delete_knowledge_base接口
        """
        response = self.post(
            "/knowledge_base/delete_knowledge_base",
            json=f"{knowledge_base_name}",
        )
        return self._get_response_value(response, as_json=True)

    def list_kb_docs(
        self,
        knowledge_base_name: str,
    ):
        """
        对应api.py/knowledge_base/list_files接口
        """
        response = self.get(
            "/knowledge_base/list_files",
            params={"knowledge_base_name": knowledge_base_name},
        )
        return self._get_response_value(
            response, as_json=True, value_func=lambda r: r.get("data", [])
        )

    def search_kb_docs(
        self,
        knowledge_base_name: str,
        query: str = "",
        top_k: int = Settings.kb_settings.VECTOR_SEARCH_TOP_K,
        score_threshold: int = Settings.kb_settings.SCORE_THRESHOLD,
        file_name: str = "",
        metadata: dict = {},
    ) -> List:
        """
        对应api.py/knowledge_base/search_docs接口
        """
        data = {
            "query": query,
            "knowledge_base_name": knowledge_base_name,
            "top_k": top_k,
            "score_threshold": score_threshold,
            "file_name": file_name,
            "metadata": metadata,
        }

        response = self.post(
            "/knowledge_base/search_docs",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def upload_kb_docs(
        self,
        files: List[Union[str, Path, bytes]],
        knowledge_base_name: str,
        override: bool = False,
        to_vector_store: bool = True,
        chunk_size=Settings.kb_settings.CHUNK_SIZE,
        chunk_overlap=Settings.kb_settings.OVERLAP_SIZE,
        zh_title_enhance=Settings.kb_settings.ZH_TITLE_ENHANCE,
        docs: Dict = {},
        not_refresh_vs_cache: bool = False,
    ):
        """
        对应api.py/knowledge_base/upload_docs接口
        """

        def convert_file(file, filename=None):
            if isinstance(file, bytes):  # raw bytes
                file = BytesIO(file)
            elif hasattr(file, "read"):  # a file io like object
                filename = filename or file.name
            else:  # a local path
                file = Path(file).absolute().open("rb")
                filename = filename or os.path.split(file.name)[-1]
            return filename, file

        files = [convert_file(file) for file in files]
        data = {
            "knowledge_base_name": knowledge_base_name,
            "override": override,
            "to_vector_store": to_vector_store,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "zh_title_enhance": zh_title_enhance,
            "docs": docs,
            "not_refresh_vs_cache": not_refresh_vs_cache,
        }

        if isinstance(data["docs"], dict):
            data["docs"] = json.dumps(data["docs"], ensure_ascii=False)
        response = self.post(
            "/knowledge_base/upload_docs",
            data=data,
            files=[("files", (filename, file)) for filename, file in files],
        )
        return self._get_response_value(response, as_json=True)

    def delete_kb_docs(
        self,
        knowledge_base_name: str,
        file_names: List[str],
        delete_content: bool = False,
        not_refresh_vs_cache: bool = False,
    ):
        """
        对应api.py/knowledge_base/delete_docs接口
        """
        data = {
            "knowledge_base_name": knowledge_base_name,
            "file_names": file_names,
            "delete_content": delete_content,
            "not_refresh_vs_cache": not_refresh_vs_cache,
        }

        response = self.post(
            "/knowledge_base/delete_docs",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def update_kb_info(self, knowledge_base_name, kb_info):
        """
        对应api.py/knowledge_base/update_info接口
        """
        data = {
            "knowledge_base_name": knowledge_base_name,
            "kb_info": kb_info,
        }

        response = self.post(
            "/knowledge_base/update_info",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def update_kb_docs(
        self,
        knowledge_base_name: str,
        file_names: List[str],
        override_custom_docs: bool = False,
        chunk_size=Settings.kb_settings.CHUNK_SIZE,
        chunk_overlap=Settings.kb_settings.OVERLAP_SIZE,
        zh_title_enhance=Settings.kb_settings.ZH_TITLE_ENHANCE,
        docs: Dict = {},
        not_refresh_vs_cache: bool = False,
    ):
        """
        对应api.py/knowledge_base/update_docs接口
        """
        data = {
            "knowledge_base_name": knowledge_base_name,
            "file_names": file_names,
            "override_custom_docs": override_custom_docs,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "zh_title_enhance": zh_title_enhance,
            "docs": docs,
            "not_refresh_vs_cache": not_refresh_vs_cache,
        }

        if isinstance(data["docs"], dict):
            data["docs"] = json.dumps(data["docs"], ensure_ascii=False)

        response = self.post(
            "/knowledge_base/update_docs",
            json=data,
        )
        return self._get_response_value(response, as_json=True)

    def recreate_vector_store(
        self,
        knowledge_base_name: str,
        allow_empty_kb: bool = True,
        vs_type: str = Settings.kb_settings.DEFAULT_VS_TYPE,
        embed_model: str = get_default_embedding(),
        chunk_size=Settings.kb_settings.CHUNK_SIZE,
        chunk_overlap=Settings.kb_settings.OVERLAP_SIZE,
        zh_title_enhance=Settings.kb_settings.ZH_TITLE_ENHANCE,
    ):
        """
        对应api.py/knowledge_base/recreate_vector_store接口
        """
        data = {
            "knowledge_base_name": knowledge_base_name,
            "allow_empty_kb": allow_empty_kb,
            "vs_type": vs_type,
            "embed_model": embed_model,
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "zh_title_enhance": zh_title_enhance,
        }

        response = self.post(
            "/knowledge_base/recreate_vector_store",
            json=data,
            stream=True,
            timeout=None,
        )
        return self._httpx_stream2generator(response, as_json=True)

    def embed_texts(
        self,
        texts: List[str],
        embed_model: str = get_default_embedding(),
        to_query: bool = False,
    ) -> List[List[float]]:
        """
        对文本进行向量化，可选模型包括本地 embed_models 和支持 embeddings 的在线模型
        """
        data = {
            "texts": texts,
            "embed_model": embed_model,
            "to_query": to_query,
        }
        resp = self.post(
            "/other/embed_texts",
            json=data,
        )
        return self._get_response_value(
            resp, as_json=True, value_func=lambda r: r.get("data")
        )

    def chat_feedback(
        self,
        message_id: str,
        score: int,
        reason: str = "",
    ) -> int:
        """
        反馈对话评价
        """
        data = {
            "message_id": message_id,
            "score": score,
            "reason": reason,
        }
        resp = self.post("/chat/feedback", json=data)
        return self._get_response_value(resp)

    def list_tools(self) -> Dict:
        """
        列出所有工具
        """
        resp = self.get("/tools")
        return self._get_response_value(
            resp, as_json=True, value_func=lambda r: r.get("data", {})
        )

    def call_tool(
        self,
        name: str,
        tool_input: Dict = {},
    ):
        """
        调用工具
        """
        data = {
            "name": name,
            "tool_input": tool_input,
        }
        resp = self.post("/tools/call", json=data)
        return self._get_response_value(
            resp, as_json=True, value_func=lambda r: r.get("data")
        )

    # Agent 运行（agent_run）相关操作 —— 任务 011
    # 接口直接返回 JSON（无 {"code","data"} 包装）；401/403 抛出 AuthenticationError；
    # 404 表示「运行不存在或无权访问」（他人运行同样按 404 处理，不泄露存在性）。

    def _agent_run_request(self, method: str, url: str, **kw) -> httpx.Response:
        fn = getattr(self, method)
        response = fn(url, retry=1, **kw)
        if response is None:
            raise AuthenticationError("无法连接API服务器")
        self._raise_for_auth(response)
        return response

    def agent_run_list(
        self, conversation_id: str = None, limit: int = 20
    ) -> List[Dict]:
        """对应 GET /agent-runs，返回当前用户的运行列表（create_time DESC）。"""
        params = {"limit": limit}
        if conversation_id:
            params["conversation_id"] = conversation_id
        r = self._agent_run_request("get", "/agent-runs", params=params)
        data = self._get_response_value(r, as_json=True)
        if isinstance(data, dict):
            return data.get("runs", [])
        return data if isinstance(data, list) else []

    def agent_run_detail(self, run_id: str) -> Dict:
        """对应 GET /agent-runs/{run_id}，返回运行详情与步骤轨迹。"""
        r = self._agent_run_request("get", f"/agent-runs/{run_id}")
        return self._get_response_value(r, as_json=True)

    def agent_run_cancel(self, run_id: str) -> Dict:
        """对应 POST /agent-runs/{run_id}/cancel，取消 pending/running 状态的运行。"""
        r = self._agent_run_request("post", f"/agent-runs/{run_id}/cancel")
        return self._get_response_value(r, as_json=True)

    # MCP Profile Methods
    def get_mcp_profile(self, **kwargs) -> Dict:
        """
        获取 MCP 通用配置
        """
        resp = self.get("/api/v1/mcp_connections/profile", **kwargs)
        return self._get_response_value(resp, as_json=True)

    def create_mcp_profile(
        self,
        timeout: int = 30,
        working_dir: str = "/tmp",
        env_vars: Dict[str, str] = None,
        **kwargs
    ) -> Dict:
        """
        创建 MCP 通用配置
        """
        if env_vars is None:
            env_vars = {}
        data = {
            "timeout": timeout,
            "working_dir": working_dir,
            "env_vars": env_vars,
        }
        resp = self.post("/api/v1/mcp_connections/profile", json=data, **kwargs)
        return self._get_response_value(resp, as_json=True)

    def update_mcp_profile(
        self,
        timeout: int = 30,
        working_dir: str = "/tmp",
        env_vars: Dict[str, str] = None,
        **kwargs
    ) -> Dict:
        """
        更新 MCP 通用配置
        """
        if env_vars is None:
            env_vars = {}
        data = {
            "timeout": timeout,
            "working_dir": working_dir,
            "env_vars": env_vars,
        }
        resp = self.put("/api/v1/mcp_connections/profile", json=data, **kwargs)
        return self._get_response_value(resp, as_json=True)

    def reset_mcp_profile(self, **kwargs) -> Dict:
        """
        重置 MCP 通用配置为默认值
        """
        resp = self.post("/api/v1/mcp_connections/profile/reset", **kwargs)
        return self._get_response_value(resp, as_json=True)

    def delete_mcp_profile(self, **kwargs) -> Dict:
        """
        删除 MCP 通用配置
        """
        resp = self.delete("/api/v1/mcp_connections/profile", **kwargs)
        return self._get_response_value(resp, as_json=True)

    # MCP Connection Methods
    def add_mcp_connection(
        self,
        server_name: str,
        args: List[str] = None,
        env: Dict[str, str] = None,
        cwd: Optional[str] = None,
        transport: str = "stdio",
        timeout: int = 30,
        enabled: bool = True,
        description: Optional[str] = None,
        config: Dict = None,
        **kwargs
    ) -> Dict:
        """
        添加 MCP 连接
        """
        if args is None:
            args = []
        if env is None:
            env = {}
        if config is None:
            config = {}
        data = {
            "server_name": server_name,
            "args": args,
            "env": env,
            "cwd": cwd,
            "transport": transport,
            "timeout": timeout,
            "enabled": enabled,
            "description": description,
            "config": config,
        }
        resp = self.post("/api/v1/mcp_connections/", json=data, **kwargs)
        return self._get_response_value(resp, as_json=True)

    def get_all_mcp_connections(self, enabled_only: bool = False, **kwargs) -> Dict:
        """
        获取所有 MCP 连接
        """
        params = {"enabled_only": enabled_only} if enabled_only else {}
        resp = self.get("/api/v1/mcp_connections/", params=params, **kwargs)
        return self._get_response_value(resp, as_json=True)

    def get_mcp_connection(self, connection_id: str, **kwargs) -> Dict:
        """
        根据 ID 获取 MCP 连接
        """
        resp = self.get(f"/api/v1/mcp_connections/{connection_id}", **kwargs)
        return self._get_response_value(resp, as_json=True)

    def update_mcp_connection(
        self,
        connection_id: str,
        server_name: Optional[str] = None,
        args: Optional[List[str]] = None,
        env: Optional[Dict[str, str]] = None,
        cwd: Optional[str] = None,
        transport: Optional[str] = None,
        timeout: Optional[int] = None,
        enabled: Optional[bool] = None,
        description: Optional[str] = None,
        config: Optional[Dict] = None,
        **kwargs
    ) -> Dict:
        """
        更新 MCP 连接
        """
        data = {}
        if server_name is not None:
            data["server_name"] = server_name
        if args is not None:
            data["args"] = args
        if env is not None:
            data["env"] = env
        if cwd is not None:
            data["cwd"] = cwd
        if transport is not None:
            data["transport"] = transport
        if timeout is not None:
            data["timeout"] = timeout
        if enabled is not None:
            data["enabled"] = enabled
        if description is not None:
            data["description"] = description
        if config is not None:
            data["config"] = config
        
        resp = self.put(f"/api/v1/mcp_connections/{connection_id}", json=data, **kwargs)
        return self._get_response_value(resp, as_json=True)

    def delete_mcp_connection(self, connection_id: str, **kwargs) -> Dict:
        """
        删除 MCP 连接
        """
        resp = self.delete(f"/api/v1/mcp_connections/{connection_id}", **kwargs)
        return self._get_response_value(resp, as_json=True)

    def enable_mcp_connection(self, connection_id: str, **kwargs) -> Dict:
        """
        启用 MCP 连接
        """
        resp = self.post(f"/api/v1/mcp_connections/{connection_id}/enable", **kwargs)
        return self._get_response_value(resp, as_json=True)

    def disable_mcp_connection(self, connection_id: str, **kwargs) -> Dict:
        """
        禁用 MCP 连接
        """
        resp = self.post(f"/api/v1/mcp_connections/{connection_id}/disable", **kwargs)
        return self._get_response_value(resp, as_json=True)

    
    def search_mcp_connections(
        self,
        keyword: Optional[str] = None,
        server_type: Optional[str] = None,
        enabled: Optional[bool] = None,
        limit: int = 50,
        **kwargs
    ) -> Dict:
        """
        搜索 MCP 连接
        """
        data = {
            "keyword": keyword,
            "server_type": server_type,
            "enabled": enabled,
            "limit": limit,
        }
        resp = self.post("/api/v1/mcp_connections/search", json=data, **kwargs)
        return self._get_response_value(resp, as_json=True)

    def get_mcp_connections_by_server_name(self, server_name: str, **kwargs) -> Dict:
        """
        根据服务器名称获取 MCP 连接
        """
        resp = self.get(f"/api/v1/mcp_connections/server/{server_name}", **kwargs)
        return self._get_response_value(resp, as_json=True)

    def get_enabled_mcp_connections(self, **kwargs) -> Dict:
        """
        获取启用的 MCP 连接
        """
        resp = self.get("/api/v1/mcp_connections/enabled/list", **kwargs)
        return self._get_response_value(resp, as_json=True)

    

class AsyncApiRequest(ApiRequest):
    def __init__(
        self,
        base_url: str = api_address(),
        timeout: float = Settings.basic_settings.HTTPX_DEFAULT_TIMEOUT,
        token: Optional[str] = None,
    ):
        super().__init__(base_url, timeout, token=token)
        self._use_async = True


def check_error_msg(data: Union[str, dict, list], key: str = "errorMsg") -> str:
    """
    return error message if error occured when requests API
    """
    if isinstance(data, dict):
        if key in data:
            return data[key]
        if "code" in data and data["code"] != 200:
            return data["msg"]
    return ""


def check_success_msg(data: Union[str, dict, list], key: str = "msg") -> str:
    """
    return error message if error occured when requests API
    """
    if (
        isinstance(data, dict)
        and key in data
        and "code" in data
        and data["code"] == 200
    ):
        return data[key]
    return ""


def get_img_base64(file_name: str) -> str:
    """
    get_img_base64 used in streamlit.
    absolute local path not working on windows.
    """
    image = f"{Settings.basic_settings.IMG_DIR}/{file_name}"
    # 读取图片
    with open(image, "rb") as f:
        buffer = BytesIO(f.read())
        base_str = base64.b64encode(buffer.getvalue()).decode()
    return f"data:image/png;base64,{base_str}"


if __name__ == "__main__":
    api = ApiRequest()
    aapi = AsyncApiRequest()

    # with api.chat_chat("你好") as r:
    #     for t in r.iter_text(None):
    #         print(t)

    # r = api.chat_chat("你好", no_remote_api=True)
    # for t in r:
    #     print(t)

    # r = api.duckduckgo_search_chat("室温超导最新研究进展", no_remote_api=True)
    # for t in r:
    #     print(t)

    # print(api.list_knowledge_bases())
