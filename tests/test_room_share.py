import asyncio,sys,time,json,importlib.util,types
from pathlib import Path
import pytest

# New independent-plugin boundaries; AuthManager is a deterministic fake here.
def test_transport_failure_has_query_error_message():
    async def scenario():
        class Client:
            async def get_room(self, **kwargs): raise plugin.RoomShareError("request_failed")
            async def get_room_data(self, **kwargs): raise plugin.RoomShareError("request_failed")
        p=plugin.create_plugin(); p._ctx=FakeCtx(); p.client=Client()
        for handler in (p._safe_get_room,p._safe_get_room_data):
            result=await handler(auto_create=False,allowed_to_create=False)
            assert result["error"]=="request_failed"
            assert "查询暂时失败" in result["reply"]
    asyncio.run(scenario())

def test_transport_failure_is_not_reported_as_expired_auth():
    async def scenario():
        class FailedAuth:
            async def request(self, *args, **kwargs):
                raise plugin.AuthError("transport unavailable")
        client = plugin.ZuojiaRoomClient(FailedAuth(), plugin.ZuojiaRoomConfig(), DummyLogger())
        with pytest.raises(plugin.RoomShareError, match="request_failed"):
            await client._request_async("/ccauthorweb/desk/spelling/getCauthorHomeid")
        class ExpiredAuth:
            async def request(self, *args, **kwargs):
                raise plugin.AuthError("authorization expired", 4001)
        client.auth_manager = ExpiredAuth()
        with pytest.raises(plugin.RoomShareError, match="auth_expired"):
            await client._request_async("/ccauthorweb/desk/spelling/getCauthorHomeid")
    asyncio.run(scenario())

class Manager:
    instances=[]
    def __init__(self, data_dir, notify_qr, **kwargs):
        self.notify_qr=notify_qr; self.closed=False; self.cancelled=False
        self.login_started=asyncio.Event(); self.release=asyncio.Event()
        self.calls=[]; self.instances.append(self)
    async def start(self): pass
    async def close(self): self.closed=True
    async def cancel_login(self): self.cancelled=True
    async def login(self):
        self.login_started.set()
        await self.notify_qr(b"\x89PNG\r\n\x1a\nfixture")
        await self.release.wait()
        return {"authenticated":True}
    async def status(self): return {"authenticated":True,"ticket":"SECRET","cookies":"SECRET"}
    async def renew(self): self.calls.append('renew')

class CaptureSend:
    def __init__(self): self.texts=[]; self.images=[]
    async def text(self,text,stream): self.texts.append((stream,text)); return True
    async def image(self,image,stream): self.images.append((stream,image)); return True


def context_for(obj,tmp_path):
    from types import SimpleNamespace
    obj._ctx=SimpleNamespace(paths=SimpleNamespace(data_dir=tmp_path),send=CaptureSend(),logger=DummyLogger())
    return obj.ctx


def test_tool_entries_deny_empty_group_and_unknown_creator():
    async def scenario():
        obj=plugin.create_plugin(); obj._ctx=FakeCtx()
        obj._plugin_config_instance=plugin.ZuojiaRoomConfig(create_room_if_missing=True,room_data_create_if_missing=True)
        async def never(**kw): raise AssertionError("blocked chat must not query")
        obj._safe_get_room=obj._safe_get_room_data=never
        for method in (obj.get_room_link,obj.get_room_data):
            result=await method(stream_id='group-stream',group_id='blocked',user_id='10001')
            assert result['error']=='chat_not_allowed'
            result=await method(stream_id='private-stream')
            assert result['error']=='chat_not_allowed'
        seen=[]
        async def no_room(**kw):
            seen.append(kw); return {'success':False,'error':'not_authorized','reply':'无权建房'}
        obj._safe_get_room=obj._safe_get_room_data=no_room
        for method in (obj.get_room_link,obj.get_room_data):
            await method(stream_id='private-stream',user_id='10001')
        assert seen and all(call['allowed_to_create'] for call in seen)
    asyncio.run(scenario())


def test_bb_defaults_allow_creation_but_keep_login_and_groups_closed():
    c=plugin.ZuojiaRoomConfig()
    assert c.create_room_if_missing and c.room_data_create_if_missing
    assert c.room_creation_permission == "allowed_chats"
    assert not plugin.is_allowed_user('admin',[])
    assert not plugin.is_allowed_group('group',[])
    assert not plugin.is_login_user('admin',plugin.ZuojiaRoomConfig(private_user_ids=['admin']))


def test_login_requires_private_admin_and_explicit_agreement(monkeypatch):
    tmp_path=Path(".")
    monkeypatch.setattr(plugin.ZuojiaRoomPlugin,"_load_dedup",lambda self: None)
    monkeypatch.setattr(plugin.ZuojiaRoomPlugin,"_persist_dedup",lambda self: None)
    monkeypatch.setattr(plugin,'AuthManager',Manager)
    async def scenario():
        obj=plugin.create_plugin(); ctx=context_for(obj,tmp_path)
        obj._plugin_config_instance=plugin.ZuojiaRoomConfig(admin_user_ids=['admin'],auth_health_check_minutes=0)
        await obj.on_load()
        args=dict(stream_id='private-stream',user_id='admin',raw_message='/拼字登录确认')
        await obj.handle_login(**args)
        assert obj._login_task is None
        await obj.handle_login(**{**args,'raw_message':'/拼字登录','group_id':'blocked-group'})
        assert not obj._terms_pending
        await obj.handle_login(**{**args,'raw_message':'/拼字登录','user_id':'outsider'})
        assert not obj._terms_pending
        await obj.handle_login(**{**args,'raw_message':'/拼字登录'})
        assert len(ctx.send.images)==0
        assert all(u in ctx.send.texts[-1][1] for u in ['202211071','202211072','142855555'])
        await obj.handle_login(**args)
        await obj.auth_manager.login_started.wait()
        await asyncio.sleep(0)
        assert len(ctx.send.images)==1 and ctx.send.images[0][0]=='private-stream'
        import base64
        assert base64.b64decode(ctx.send.images[0][1]).startswith(b'\x89PNG')
        await obj.handle_login(**{**args,'raw_message':'/拼字授权状态'})
        assert 'SECRET' not in str(ctx.send.texts)
        manager=obj.auth_manager
        await obj.handle_login(**{**args,'raw_message':'/拼字取消登录'})
        assert obj._login_task is None and manager.cancelled
        await obj.on_unload()
        assert manager.closed
    asyncio.run(scenario())


def test_config_reload_cancels_login_fallback_and_rebuilds(monkeypatch):
    tmp_path=Path(".")
    monkeypatch.setattr(plugin.ZuojiaRoomPlugin,"_load_dedup",lambda self: None)
    monkeypatch.setattr(plugin.ZuojiaRoomPlugin,"_persist_dedup",lambda self: None)
    monkeypatch.setattr(plugin,'AuthManager',Manager)
    async def scenario():
        obj=plugin.create_plugin(); context_for(obj,tmp_path)
        obj._plugin_config_instance=plugin.ZuojiaRoomConfig(admin_user_ids=['admin'],auth_health_check_minutes=1)
        await obj.on_load(); manager=obj.auth_manager; health=obj._auth_health_task
        args=dict(stream_id='private-stream',user_id='admin')
        await obj.handle_login(**args,raw_message='/拼字登录')
        await obj.handle_login(**args,raw_message='/拼字登录确认')
        await manager.login_started.wait(); login=obj._login_task
        fallback=asyncio.create_task(asyncio.sleep(1000)); obj._pending_fallbacks['x']=(fallback,1)
        await obj.on_config_update(plugin.CONFIG_RELOAD_SCOPE_SELF,{'plugin':{'enabled':True,'config_version':'0.1.0'},'admin_user_ids':['new'],'auth_health_check_minutes':0},'0.1.0')
        assert manager.closed and manager.cancelled and login.done() and fallback.done() and health.done()
        assert obj.auth_manager is not manager and not obj._terms_pending
        assert obj.config.admin_user_ids==['new']
        await obj.on_unload(); assert obj.auth_manager is None
    asyncio.run(scenario())


def test_room_requests_use_only_authmanager_and_sanitize_errors():
    class Auth:
        async def request(self,method,path,**kw):
            self.call=(method,path,kw); return {'returnCode':2000,'result':{'homeId':0}}
    async def scenario():
        auth=Auth(); c=plugin.ZuojiaRoomClient(auth,plugin.ZuojiaRoomConfig(),DummyLogger())
        assert await c.get_current_room()==(0,0)
        assert auth.call[0]=='GET' and auth.call[1].startswith('/ccauthorweb/')
        assert not hasattr(c,'auth') and not hasattr(c,'_request')
        await c._request_async('/ccauthorweb/desk/spelling/createhome',method='POST',json_body={'numbersLimit':50})
        assert json.loads(auth.call[2]['data'])=={'numbersLimit':50}
        async def fail(*a,**k): raise plugin.AuthError('SECRET')
        auth.request=fail
        with pytest.raises(plugin.RoomShareError) as error: await c.get_current_room()
        assert str(error.value)=='request_failed' and 'SECRET' not in str(error.value)
    asyncio.run(scenario())


def test_group_auth_failure_is_never_no_room():
    async def scenario():
        obj=_make_keyword_obj(keyword_mode='llm_first')
        async def fail(**kw): return {'success':False,'error':'auth_expired','reply':obj.config.auth_expired_notice_template}
        obj._safe_get_room=fail
        reply=await obj._build_keyword_reply('s','123456','member','有房吗')
        assert reply==obj.config.auth_expired_notice_template
    asyncio.run(scenario())

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
# Prefer actual installed SDK; only stub when unavailable.
try:
    import maibot_sdk
except ImportError:
    from pydantic import BaseModel,Field
    sdk=types.ModuleType('maibot_sdk')
    class BasePlugin:
        def __init__(self): self._plugin_config_instance=self.config_model()
        @property
        def config(self): return self._plugin_config_instance
        @property
        def ctx(self): return self._ctx
        def set_plugin_config(self,data): self._plugin_config_instance=self.config_model.model_validate(data)
    def decorator(*a,**k): return lambda fn: fn
    for k,v in dict(CONFIG_RELOAD_SCOPE_SELF='self',Command=decorator,HookHandler=decorator,Tool=decorator,Field=Field,PluginConfigBase=BaseModel,MaiBotPlugin=BasePlugin).items(): setattr(sdk,k,v)
    ty=types.ModuleType('maibot_sdk.types')
    class Modes:
        SKIP='skip'
        BLOCKING='blocking'
        CONTINUE='continue'
        STRING='string'
    ty.ErrorPolicy=ty.HookMode=ty.ToolParamType=Modes
    ty.ToolParameterInfo=lambda **kw: kw
    sys.modules['maibot_sdk']=sdk
    sys.modules['maibot_sdk.types']=ty
try:
    import pinzi_auth
except ImportError:
    auth=types.ModuleType('pinzi_auth')
    auth.AuthError=type('AuthError',(RuntimeError,),{})
    auth.AuthManager=object
    sys.modules['pinzi_auth']=auth
spec=importlib.util.spec_from_file_location('room_plugin_under_test',ROOT/'plugin.py')
plugin=importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)

def test_match_keyword_hits_chinese_phrases():
    assert plugin.match_keyword("群里有码字房吗", ["有房吗", "码字房"])
    assert plugin.match_keyword("有房吗", plugin.DEFAULT_KEYWORDS)
    assert plugin.match_keyword("拼字房间号是多少", plugin.DEFAULT_KEYWORDS)


def test_match_keyword_misses_unrelated_text():
    assert not plugin.match_keyword("今天天气不错", plugin.DEFAULT_KEYWORDS)


def test_as_text_handles_segment_data_as_string():
    assert plugin._as_text([{"data": "有房吗"}, {"data": {"text": "码字房"}}]) == "有房吗 码字房"


def test_normalize_group_id():
    assert plugin.normalize_group_id("group_123456") == "123456"
    assert plugin.normalize_group_id("gid_123456") == "123456"
    assert plugin.normalize_group_id("123456") == "123456"


def test_is_allowed_group():
    assert plugin.is_allowed_group("123456", ["123456"])
    assert plugin.is_allowed_group("group_123456", ["123456"])
    assert not plugin.is_allowed_group("999", ["123456"])



def test_is_allowed_user_checks_admin_list():
    assert plugin.is_allowed_user("10001", ["10001"])
    assert not plugin.is_allowed_user("10002", ["10001"])



def test_is_allowed_chat_group_uses_group_whitelist():
    config = plugin.ZuojiaRoomConfig(
        group_whitelist=["123456"],
        private_chat_enabled=True,
    )
    assert plugin.is_allowed_chat("123456", "10001", config)
    assert not plugin.is_allowed_chat("999999", "10001", config)


def test_is_allowed_chat_private_disabled():
    config = plugin.ZuojiaRoomConfig(private_chat_enabled=False)
    assert not plugin.is_allowed_chat("", "10001", config)


def test_is_allowed_chat_private_empty_allows_all():
    config = plugin.ZuojiaRoomConfig(
        private_chat_enabled=True,
        private_user_ids=[],
    )
    assert plugin.is_allowed_chat("", "10001", config)


def test_is_allowed_chat_private_user_whitelist():
    config = plugin.ZuojiaRoomConfig(
        private_chat_enabled=True,
        private_user_ids=["10001"],
    )
    assert plugin.is_allowed_chat("", "10001", config)
    assert not plugin.is_allowed_chat("", "10002", config)


def test_trigger_enabled():
    assert plugin.trigger_enabled("keyword", ["keyword", "command"])
    assert not plugin.trigger_enabled("tool", ["keyword", "command"])
    assert plugin.trigger_enabled("COMMAND", plugin.DEFAULT_TRIGGER_MODES)


def test_dig_path():
    data = {"data": {"room": {"number": "A1", "link": "https://example.com/join"}}}
    assert plugin.dig_path(data, "data.room.number") == "A1"


def test_parse_room_info_uses_configured_fields():
    data = {"data": {"room_number": "A1", "room_link": "https://example.com/join"}}
    room = plugin.parse_room_info(data, "data.room_number", "data.room_link")
    assert room == {
        "room_number": "A1",
        "room_link": "https://example.com/join",
        "source": "api",
    }


def test_build_reply_uses_template():
    room = {"room_number": "A1", "room_link": "https://example.com/join"}
    assert plugin.build_reply(room, "拼字房来了：{room_number} {room_link}") == (
        "拼字房来了：A1 https://example.com/join"
    )


def test_build_reply_can_include_creator_name():
    room = {
        "room_number": "A1",
        "room_link": "https://example.com/join",
        "creator_name": "房主甲",
        "creator_id": "10001",
    }
    assert plugin.build_reply(
        room, "拼字房来了：{room_number} {room_link} 创建人：{creator_name}"
    ) == "拼字房来了：A1 https://example.com/join 创建人：房主甲"


def test_build_reply_official_invite_template():
    room = {
        "room_number": "19705967",
        "room_link": "https://m.write.qq.com/portal/spellInvite/6KbSh9t0",
        "creator_name": "BB大小姐",
        "target_words": "30000",
    }
    assert plugin.build_reply(room, plugin.DEFAULT_REPLY_TEMPLATE) == (
        "【BB大小姐】邀请你加入30000字拼字。\n"
        "https://m.write.qq.com/portal/spellInvite/6KbSh9t0\n"
        "或进入作家助手-拼字星球，搜索房间号：\n"
        "19705967"
    )


def test_extract_room_creator_picks_user_flag_one():
    data = {
        "result": {
            "spellingHomeUserInfoVos": [
                {"cauthorid": "20002", "name": "路人", "userFlag": 2},
                {"cauthorid": "10001", "name": "房主甲", "userFlag": 1},
            ]
        }
    }
    assert plugin.extract_room_creator(data) == {
        "creator_id": "10001",
        "creator_name": "房主甲",
        "creator_cauthorid": "10001",
    }


def test_extract_room_creator_handles_missing_list():
    assert plugin.extract_room_creator({"result": {}}) == {}
    assert plugin.extract_room_creator({"result": None}) == {}


def test_extract_room_details_includes_target_goal():
    data = {
        "result": {
            "spellingHomeUserInfoVos": [
                {"cauthorid": "10001", "name": "房主甲", "userFlag": 1}
            ],
            "targetwords": 30000,
            "targetminute": 60,
        }
    }
    assert plugin.extract_room_details(data) == {
        "creator_id": "10001",
        "creator_name": "房主甲",
        "creator_cauthorid": "10001",
        "target_words": "30000",
        "target_minutes": "60",
    }


def test_room_data_config_defaults():
    config = plugin.ZuojiaRoomConfig()
    assert config.room_data_create_if_missing is True
    assert config.room_data_rank_limit == 13
    assert config.room_data_no_room_message


def test_extract_room_members_handles_word_count_fields():
    data = {
        "result": {
            "spellingHomeUserInfoVos": [
                {"cauthorid": "1", "name": "甲", "wordCount": 30002},
                {"cauthorid": "2", "name": "乙", "words": 27090},
                {"cauthorid": "3", "name": "丙", "writeWords": 26638},
                {"cauthorid": "4", "name": "丁", "codeWords": 23047},
                {"cauthorid": "5", "name": "戊", "totalWords": 19028},
                {"cauthorid": "6", "name": "己", "noField": 999},
            ]
        }
    }
    members = plugin.extract_room_members(data)
    assert [member["words"] for member in members] == [
        30002,
        27090,
        26638,
        23047,
        19028,
        0,
    ]


def test_extract_room_ranking_members_uses_num():
    data = {
        "result": {
            "spellingHomeRankingInfoVos": [
                {"cauthorId": "1", "name": "甲", "num": "30002"},
                {"cauthorId": "2", "name": "乙", "num": "27090"},
                {"cauthorId": "3", "name": "丙", "num": 0},
            ]
        }
    }
    members = plugin.extract_room_ranking_members(data)
    assert [member["words"] for member in members] == [30002, 27090, 0]
    assert members[0]["cauthorid"] == "1"


def test_merge_room_members_prefers_detail_num():
    base = [
        {"cauthorid": "1", "name": "甲", "words": 0},
        {"cauthorid": "2", "name": "乙", "words": 0},
        {"cauthorid": "3", "name": "丙", "words": 0},
    ]
    detail = [
        {"cauthorid": "1", "name": "甲", "words": 30002},
        {"cauthorid": "2", "name": "乙", "words": 27090},
        {"cauthorid": "4", "name": "丁", "words": 19028},
    ]
    merged = plugin.merge_room_members(base, detail)
    assert [member["name"] for member in merged] == ["甲", "乙", "丙", "丁"]
    assert [member["words"] for member in merged] == [30002, 27090, 0, 19028]


def test_format_room_rank_list_official_style_with_ties():
    members = [
        {"name": "挤饱", "words": 30002},
        {"name": "华夏狼皇", "words": 27090},
        {"name": "零字一", "words": 0},
        {"name": "零字二", "words": 0},
    ]
    lines = plugin.format_room_rank_list(members, limit=13)
    assert lines == [
        "第1名--挤饱--已码30002字",
        "第2名--华夏狼皇--已码27090字",
        "第3名--零字一--已码0字",
        "第3名--零字二--已码0字",
    ]


def test_format_room_rank_list_respects_limit():
    members = [
        {"name": f"作者{n}", "words": 1000 - n}
        for n in range(3)
    ]
    assert plugin.format_room_rank_list(members, limit=2) == [
        "第1名--作者0--已码1000字",
        "第2名--作者1--已码999字",
    ]


def test_extract_message_context_handles_hook_message():
    kwargs = {
        "message": {
            "group_id": "123456",
            "user_id": "10001",
            "raw_message": "有房吗",
        },
        "chat_id": "chat-1",
    }
    stream_id, group_id, user_id, raw = plugin.extract_message_context(kwargs)
    assert stream_id == "chat-1"
    assert group_id == "123456"
    assert user_id == "10001"
    assert raw == "有房吗"


def test_extract_message_context_handles_real_maibot_hook_payload():
    kwargs = {
        "message": {
            "message_id": "m1",
            "session_id": "qq_group_123456",
            "message_info": {
                "user_info": {
                    "user_id": "10001",
                    "user_nickname": "测试用户",
                    "user_cardname": None,
                },
                "group_info": {
                    "group_id": "123456",
                    "group_name": "测试群",
                },
                "additional_config": {"self_id": "888888"},
            },
            "raw_message": [
                {"type": "at", "data": {"target_user_id": "888888"}},
                {"type": "text", "data": "来个码字房"},
            ],
        },
    }
    stream_id, group_id, user_id, raw = plugin.extract_message_context(kwargs)
    assert stream_id == "qq_group_123456"
    assert group_id == "123456"
    assert user_id == "10001"
    assert "码字房" in raw
    assert plugin.is_allowed_chat(
        group_id,
        user_id,
        plugin.ZuojiaRoomConfig(
            group_whitelist=["123456"],
            private_chat_enabled=True,
        ),
    )


def test_is_self_message_detects_self_id():
    kwargs = {
        "message": {
            "message_info": {
                "user_info": {"user_id": "888888"},
                "additional_config": {"self_id": "888888"},
            }
        }
    }
    assert plugin.is_self_message(kwargs, "888888")
    assert not plugin.is_self_message(kwargs, "10001")
    assert not plugin.is_self_message(kwargs, "")


def test_resolve_self_id_from_hook_payload():
    kwargs = {
        "message": {
            "message_info": {
                "user_info": {"user_id": "10001"},
                "additional_config": {"self_id": "888888"},
            }
        }
    }
    assert plugin.resolve_self_id(kwargs) == "888888"
    assert plugin.resolve_self_id({"message": {"self_id": "777"}}) == "777"
    assert plugin.resolve_self_id({"message": {}}) == ""


def test_extract_at_target_ids_supports_maibot_and_onebot_data():
    kwargs = {
        "message": {
            "raw_message": [
                {"type": "at", "data": {"target_user_id": "111111"}},
                {"type": "at", "data": {"qq": "222222"}},
                {"type": "at", "data": {"target_user_id": "111111"}},
                {"type": "text", "data": "有房吗"},
            ]
        }
    }
    assert plugin.extract_at_target_ids(kwargs) == ["111111", "222222"]


def test_extract_at_target_ids_handles_string_raw_message():
    assert plugin.extract_at_target_ids({"message": {"raw_message": "有房吗"}}) == []
    assert plugin.extract_at_target_ids({"message": {"raw_message": None}}) == []


def test_message_addresses_other():
    # 没 @ 任何人 → 不算 @ 别人
    assert not plugin.message_addresses_other({"message": {"raw_message": [{"type": "text", "data": "有房吗"}]}}, "888888")
    # @ 本机器人 → 不算 @ 别人
    assert not plugin.message_addresses_other(
        {"message": {"raw_message": [{"type": "at", "data": {"target_user_id": "888888"}}]}}, "888888"
    )
    # @ 别人 → 算
    assert plugin.message_addresses_other(
        {"message": {"raw_message": [{"type": "at", "data": {"target_user_id": "123456"}}]}}, "888888"
    )
    # 同时 @ 别人和本机器人 → 不算（消息也是给 BB 的）
    assert not plugin.message_addresses_other(
        {
            "message": {
                "raw_message": [
                    {"type": "at", "data": {"target_user_id": "123456"}},
                    {"type": "at", "data": {"target_user_id": "888888"}},
                ]
            }
        },
        "888888",
    )
    # self_id 未知但消息有 at → 保守视为 @ 别人
    assert plugin.message_addresses_other(
        {"message": {"raw_message": [{"type": "at", "data": {"target_user_id": "123456"}}]}}, ""
    )


def test_on_message_before_process_aborts_for_real_hook_payload():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
        group_whitelist=["123456"],
        trigger_modes=["keyword"],
        keyword_reply_mode="always",
        create_room_if_missing=False,
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.invite_share_path: {"result": "gTwXA1qg"},
            config.home_user_list_path: {
                "result": {
                    "targetwords": 30000,
                    "targetminute": 60,
                    "spellingHomeUserInfoVos": [
                        {"cauthorid": "12345678901234567", "name": "房主甲", "userFlag": 1}
                    ]
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    kwargs = {
        "message": {
            "message_id": "m1",
            "session_id": "qq_group_123456",
            "message_info": {
                "user_info": {
                    "user_id": "10001",
                    "user_nickname": "测试用户",
                    "user_cardname": None,
                },
                "group_info": {
                    "group_id": "123456",
                    "group_name": "测试群",
                },
                "additional_config": {"self_id": "888888"},
            },
            "raw_message": [
                {"type": "at", "data": {"target_user_id": "888888"}},
                {"type": "text", "data": "来个码字房"},
            ],
            "processed_plain_text": "来个码字房",
        },
    }

    result = asyncio.run(obj.on_message_before_process(**kwargs))
    assert result == {"action": "abort"}
    assert obj.ctx.send.texts == ["qq_group_123456"]


def test_on_message_before_process_ignores_self_message():
    config = plugin.ZuojiaRoomConfig(
        group_whitelist=["123456"],
        trigger_modes=["keyword"],
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj._ctx = FakeCtx()

    kwargs = {
        "message": {
            "message_id": "m1",
            "session_id": "qq_group_123456",
            "message_info": {
                "user_info": {
                    "user_id": "888888",
                    "user_nickname": "BB",
                    "user_cardname": None,
                },
                "group_info": {
                    "group_id": "123456",
                    "group_name": "测试群",
                },
                "additional_config": {"self_id": "888888"},
            },
            "raw_message": [
                {"type": "text", "data": "【BB】邀请你加入30000字拼字。\nhttps://m.write.qq.com/portal/spellInvite/abc"},
            ],
            "processed_plain_text": "【BB】邀请你加入30000字拼字。",
        },
    }

    result = asyncio.run(obj.on_message_before_process(**kwargs))
    assert result == {"action": "continue"}
    assert obj.ctx.send.texts == []


def test_extract_home_id_from_current_response():
    data = {"returnCode": 2000, "result": {"homeId": 19693024}}
    assert plugin.extract_home_id_from_current_response(data) == 19693024
    assert plugin.extract_home_id_from_current_response({"result": {"homeId": 0}}) == 0
    assert plugin.extract_home_id_from_current_response({"result": None}) == 0


def test_extract_home_id_from_create_response():
    assert plugin.extract_home_id_from_create_response(
        {"returnCode": 2000, "result": {"homeid": 123}}
    ) == "123"
    assert plugin.extract_home_id_from_create_response(
        {"returnCode": 2000, "result": {"homeId": 456}}
    ) == "456"


def test_build_share_link():
    assert (
        plugin.build_share_link("https://m.write.qq.com/portal/spellInvite", "abc")
        == "https://m.write.qq.com/portal/spellInvite/abc"
    )
    assert (
        plugin.build_share_link("https://m.write.qq.com/portal/spellInvite/", "xyz")
        == "https://m.write.qq.com/portal/spellInvite/xyz"
    )


def test_build_create_room_payload_defaults():
    config = plugin.ZuojiaRoomConfig()
    payload = plugin.build_create_room_payload(config)
    assert payload["worldLimit"] == 30000
    assert payload["timeLimit"] == 60
    assert payload["homeType"] == 2
    assert payload["numbersLimit"] == 50


def test_build_create_room_payload_override_member_limit():
    config = plugin.ZuojiaRoomConfig(room_default_member_limit=80)
    payload = plugin.build_create_room_payload(config)
    assert payload["numbersLimit"] == 80


def test_create_room_defaults_is_hidden_from_webui():
    field_info = plugin.ZuojiaRoomConfig.model_fields["create_room_defaults"]
    json_extra = field_info.json_schema_extra or {}
    assert json_extra.get("hidden") is True



class DummyLogger:
    def __init__(self):
        self.warnings = []

    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        self.warnings.append(" ".join(str(item) for item in args))

    def exception(self, *args, **kwargs):
        pass



class FakePaths:
    """对齐真实 ctx.paths（插件用 ctx.paths.data_dir 定位 auth.json）。"""

    def __init__(self, data_dir):
        self.data_dir = data_dir


class FakeCtx:
    def __init__(self, api=None, data_dir=None):
        self.logger = DummyLogger()
        self.send = FakeSend()
        self.api = api
        self.paths = FakePaths(data_dir if data_dir is not None else Path("."))


class FakeSend:
    def __init__(self):
        self.texts = []
        self.images = []

    async def text(self, text, stream_id):
        self.texts.append(stream_id)

    async def image(self, image_b64, stream_id):
        self.images.append(stream_id)


class FakeClient(plugin.ZuojiaRoomClient):
    def __init__(self, config, responses):
        super().__init__(None, config, DummyLogger())
        self.responses = responses

    async def _request_async(self, path, method="GET", params=None, json_body=None):
        response = self.responses.get(path)
        if response is None:
            raise plugin.RoomShareError(f"no fake response for {path}")
        if callable(response):
            response = response(method, params, json_body)
        return response


def test_client_get_room_existing_returns_share_link():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.invite_share_path: {"result": "gTwXA1qg"},
            config.home_user_list_path: {
                "result": {
                    "targetwords": 30000,
                    "targetminute": 60,
                    "spellingHomeUserInfoVos": [
                        {"cauthorid": "12345678901234567", "name": "房主甲", "userFlag": 1}
                    ]
                }
            },
        },
    )
    room = asyncio.run(client.get_room(auto_create=False, allowed_to_create=False))
    assert room["room_number"] == "19693024"
    assert room["room_link"] == "https://m.write.qq.com/portal/spellInvite/gTwXA1qg"
    assert room["creator_name"] == "房主甲"
    assert room["target_words"] == "30000"
    assert room["target_minutes"] == "60"


def test_client_get_room_no_room_and_not_auto_create_raises():
    config = plugin.ZuojiaRoomConfig(api_base_url="https://pcwrite.yuewen.com")
    client = FakeClient(config, {config.current_room_path: {"result": {"homeId": 0}}})
    try:
        asyncio.run(client.get_room(auto_create=False, allowed_to_create=False))
    except plugin.RoomShareError as exc:
        assert str(exc) == "no_room"
    else:
        raise AssertionError("expected no_room error")


def test_client_get_room_no_room_and_not_authorized_raises():
    config = plugin.ZuojiaRoomConfig(api_base_url="https://pcwrite.yuewen.com")
    client = FakeClient(config, {config.current_room_path: {"result": {"homeId": 0}}})
    try:
        asyncio.run(client.get_room(auto_create=True, allowed_to_create=False))
    except plugin.RoomShareError as exc:
        assert str(exc) == "not_authorized"
    else:
        raise AssertionError("expected not_authorized error")


def test_client_creates_room_when_missing_and_allowed():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 0}},
            config.create_room_path: {"result": {"homeid": 19693123}},
            config.invite_share_path: {"result": "new-share"},
            config.home_user_list_path: {"result": {"spellingHomeUserInfoVos": []}},
        },
    )
    room = asyncio.run(client.get_room(auto_create=True, allowed_to_create=True))
    assert room["room_number"] == "19693123"
    assert room["room_link"] == "https://m.write.qq.com/portal/spellInvite/new-share"


def test_client_share_requires_author_id():
    config = plugin.ZuojiaRoomConfig(api_base_url="https://pcwrite.yuewen.com")
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.invite_share_path: {"result": "gTwXA1qg"},
        },
    )
    try:
        asyncio.run(client.get_room(auto_create=False, allowed_to_create=False))
    except plugin.RoomShareError as exc:
        assert "author_id" in str(exc)
    else:
        raise AssertionError("expected author_id error")


def test_client_get_room_data_existing_returns_members():
    config = plugin.ZuojiaRoomConfig(api_base_url="https://pcwrite.yuewen.com")
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.home_user_list_path: {
                "result": {
                    "spellingHomeUserInfoVos": [
                        {"name": "甲", "wordCount": 30002},
                        {"name": "乙", "wordCount": 27090},
                    ]
                }
            },
        },
    )
    result = asyncio.run(client.get_room_data(auto_create=False, allowed_to_create=False))
    assert result["home_id"] == 19693024
    assert result["created"] is False
    assert [member["name"] for member in result["members"]] == ["甲", "乙"]


def test_client_get_room_data_existing_merges_detail_num():
    config = plugin.ZuojiaRoomConfig(api_base_url="https://pcwrite.yuewen.com")
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.home_user_list_path: {
                "result": {
                    "spellingHomeUserInfoVos": [
                        {"cauthorid": "1", "name": "甲"},
                        {"cauthorid": "2", "name": "乙"},
                    ]
                }
            },
            config.home_detail_path: {
                "result": {
                    "spellingHomeRankingInfoVos": [
                        {"cauthorId": "1", "name": "甲", "num": "30002"},
                        {"cauthorId": "2", "name": "乙", "num": "27090"},
                    ]
                }
            },
        },
    )
    result = asyncio.run(client.get_room_data(auto_create=False, allowed_to_create=False))
    assert result["home_id"] == 19693024
    assert [member["words"] for member in result["members"]] == [30002, 27090]


def test_client_get_room_data_no_room_and_not_auto_create_raises():
    config = plugin.ZuojiaRoomConfig(api_base_url="https://pcwrite.yuewen.com")
    client = FakeClient(config, {config.current_room_path: {"result": {"homeId": 0}}})
    try:
        asyncio.run(client.get_room_data(auto_create=False, allowed_to_create=False))
    except plugin.RoomShareError as exc:
        assert str(exc) == "no_room"
    else:
        raise AssertionError("expected no_room error")


def test_client_get_room_data_no_room_creates_room_when_allowed():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 0}},
            config.create_room_path: {"result": {"homeid": 19693123}},
            config.invite_share_path: {"result": "new-share"},
            config.home_user_list_path: {"result": {"spellingHomeUserInfoVos": []}},
        },
    )
    result = asyncio.run(client.get_room_data(auto_create=True, allowed_to_create=True))
    assert result["created"] is True
    assert result["room"]["room_number"] == "19693123"
    assert result["room"]["room_link"] == "https://m.write.qq.com/portal/spellInvite/new-share"


def test_safe_room_data_auth_expired_reply_is_not_no_room():
    config = plugin.ZuojiaRoomConfig(api_base_url="https://pcwrite.yuewen.com")

    def boom(method, params, json_body):
        raise plugin.RoomShareError("接口 HTTP 400")

    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.home_user_list_path: boom,
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    result = asyncio.run(obj._safe_get_room_data(auto_create=False, allowed_to_create=False))
    assert result["success"] is False
    assert result["reply"] == config.auth_expired_notice_template
    assert "暂时没有拼字房" not in result["reply"]



def test_get_room_link_sends_text_to_injected_stream_id():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.invite_share_path: {"result": "gTwXA1qg"},
            config.home_user_list_path: {
                "result": {
                    "targetwords": 30000,
                    "targetminute": 60,
                    "spellingHomeUserInfoVos": [
                        {"cauthorid": "12345678901234567", "name": "房主甲", "userFlag": 1}
                    ]
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    result = asyncio.run(obj.get_room_link(stream_id="chat-real-1", user_id="10001"))
    assert result["success"] is True
    assert obj.ctx.send.texts == ["chat-real-1"]


def test_get_room_data_sends_text_to_injected_stream_id():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.home_user_list_path: {
                "result": {
                    "spellingHomeUserInfoVos": [
                        {"name": "甲", "wordCount": 30002},
                        {"name": "乙", "wordCount": 27090},
                    ]
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    result = asyncio.run(obj.get_room_data(stream_id="chat-real-1", user_id="10001"))
    assert result["success"] is True
    assert obj.ctx.send.texts == ["chat-real-1"]


def test_get_room_link_does_not_ask_llm_to_reply_again():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.invite_share_path: {"result": "gTwXA1qg"},
            config.home_user_list_path: {
                "result": {
                    "targetwords": 30000,
                    "targetminute": 60,
                    "spellingHomeUserInfoVos": [
                        {"cauthorid": "12345678901234567", "name": "房主甲", "userFlag": 1}
                    ]
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    result = asyncio.run(obj.get_room_link(stream_id="chat-real-1", user_id="10001"))
    assert result["success"] is True
    assert result["content"] == plugin.TOOL_ALREADY_SENT_ROOM_LINK
    assert "reply" not in result
    assert obj.ctx.send.texts == ["chat-real-1"]


def test_get_room_data_does_not_ask_llm_to_reply_again():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.home_user_list_path: {
                "result": {
                    "spellingHomeUserInfoVos": [
                        {"name": "甲", "wordCount": 30002},
                        {"name": "乙", "wordCount": 27090},
                    ]
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    result = asyncio.run(obj.get_room_data(stream_id="chat-real-1", user_id="10001"))
    assert result["success"] is True
    assert result["created"] is False
    assert result["content"] == plugin.TOOL_ALREADY_SENT_ROOM_DATA
    assert "reply" not in result
    assert obj.ctx.send.texts == ["chat-real-1"]


def test_get_room_link_respects_cooldown_and_reports_already_sent():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.invite_share_path: {"result": "gTwXA1qg"},
            config.home_user_list_path: {
                "result": {
                    "targetwords": 30000,
                    "targetminute": 60,
                    "spellingHomeUserInfoVos": [
                        {"cauthorid": "12345678901234567", "name": "房主甲", "userFlag": 1}
                    ]
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    # 第一次工具调用正常发送
    result1 = asyncio.run(obj.get_room_link(stream_id="chat-real-1", user_id="10001"))
    assert result1["success"] is True
    assert result1["content"] == plugin.TOOL_ALREADY_SENT_ROOM_LINK
    assert obj.ctx.send.texts == ["chat-real-1"]

    # 冷却期内第二次工具调用不再发送，但仍按“已发送”返回，避免模型补话
    result2 = asyncio.run(obj.get_room_link(stream_id="chat-real-1", user_id="10001"))
    assert result2["success"] is True
    assert result2["content"] == plugin.TOOL_ALREADY_SENT_ROOM_LINK
    assert "reply" not in result2
    assert obj.ctx.send.texts == ["chat-real-1"]


def test_get_room_data_respects_cooldown_and_reports_already_sent():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.home_user_list_path: {
                "result": {
                    "spellingHomeUserInfoVos": [
                        {"name": "甲", "wordCount": 30002},
                        {"name": "乙", "wordCount": 27090},
                    ]
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    result1 = asyncio.run(obj.get_room_data(stream_id="chat-real-1", user_id="10001"))
    assert result1["success"] is True
    assert result1["created"] is False
    assert result1["content"] == plugin.TOOL_ALREADY_SENT_ROOM_DATA
    assert obj.ctx.send.texts == ["chat-real-1"]

    result2 = asyncio.run(obj.get_room_data(stream_id="chat-real-1", user_id="10001"))
    assert result2["success"] is True
    assert result2["created"] is False
    assert result2["content"] == plugin.TOOL_ALREADY_SENT_ROOM_DATA
    assert "reply" not in result2
    assert obj.ctx.send.texts == ["chat-real-1"]


def test_get_room_link_without_stream_id_returns_reply_for_llm():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.invite_share_path: {"result": "gTwXA1qg"},
            config.home_user_list_path: {
                "result": {
                    "targetwords": 30000,
                    "targetminute": 60,
                    "spellingHomeUserInfoVos": [
                        {"cauthorid": "12345678901234567", "name": "房主甲", "userFlag": 1}
                    ]
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    result = asyncio.run(obj.get_room_link(stream_id="", user_id="10001"))
    assert result["success"] is True
    assert "content" not in result
    assert "reply" in result
    assert obj.ctx.send.texts == []


def test_get_room_link_failure_also_does_not_ask_llm_to_reply_again():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {config.current_room_path: {"result": {"homeId": 0}}},
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    result = asyncio.run(obj.get_room_link(stream_id="chat-real-1", user_id="10001"))
    assert result["success"] is False
    assert result["content"] == plugin.TOOL_ALREADY_SENT_FAILURE
    assert "reply" not in result
    assert obj.ctx.send.texts == ["chat-real-1"]


# ─── keyword_reply_mode / 冷却 / 去重 / llm_first 兜底 ─────────────────


def test_keyword_reply_mode_defaults_to_llm_first():
    config = plugin.ZuojiaRoomConfig()
    assert config.keyword_reply_mode == "llm_first"
    assert config.keyword_cooldown_seconds == 120.0
    assert config.fallback_delay_seconds == 20.0
    assert config.reply_dedup_seconds == 10.0
    assert config.keyword_patterns == []
    assert config.ignore_at_to_others is True


def test_resolve_keyword_reply_mode_accepts_valid_and_falls_back():
    assert plugin.resolve_keyword_reply_mode(plugin.ZuojiaRoomConfig()) == "llm_first"
    assert (
        plugin.resolve_keyword_reply_mode(plugin.ZuojiaRoomConfig(keyword_reply_mode="llm_first"))
        == "llm_first"
    )
    assert (
        plugin.resolve_keyword_reply_mode(plugin.ZuojiaRoomConfig(keyword_reply_mode="TOOL_ONLY"))
        == "tool_only"
    )
    assert (
        plugin.resolve_keyword_reply_mode(plugin.ZuojiaRoomConfig(keyword_reply_mode="bogus"))
        == "always"
    )


def test_match_keyword_patterns_matches_regex_and_skips_bad():
    assert plugin.match_keyword_patterns("来个码字房", ["码字房"])
    assert plugin.match_keyword_patterns("来个码字房", ["^来个"])
    assert not plugin.match_keyword_patterns("今天天气不错", ["有房吗"])
    # 非法正则不影响其它正则
    assert plugin.match_keyword_patterns("来个码字房", ["[invalid", "码字房"])
    assert not plugin.match_keyword_patterns("今天天气不错", ["[invalid"])


def test_should_dedupe_outbound_repeat():
    now = 1000.0
    assert plugin.should_dedupe_outbound_repeat("abc", "abc", now - 1, 5, now)
    assert not plugin.should_dedupe_outbound_repeat("abc", "abc", now - 6, 5, now)
    assert not plugin.should_dedupe_outbound_repeat("abc", "abd", now - 1, 5, now)
    assert not plugin.should_dedupe_outbound_repeat("", "abc", now - 1, 5, now)
    assert not plugin.should_dedupe_outbound_repeat("abc", None, None, 5, now)


def _keyword_kwargs():
    return {
        "message": {
            "message_id": "m1",
            "session_id": "qq_group_123456",
            "message_info": {
                "user_info": {"user_id": "10001", "user_nickname": "测试用户", "user_cardname": None},
                "group_info": {"group_id": "123456", "group_name": "测试群"},
                "additional_config": {"self_id": "888888"},
            },
            "raw_message": [
                {"type": "at", "data": {"target_user_id": "888888"}},
                {"type": "text", "data": "来个码字房"},
            ],
            "processed_plain_text": "来个码字房",
        },
    }


def _make_keyword_obj(keyword_mode="always", **overrides):
    config_kwargs = dict(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
        group_whitelist=["123456"],
        trigger_modes=["keyword"],
        keyword_reply_mode=keyword_mode,
    )
    config_kwargs.setdefault("fallback_delay_seconds", 0.0)
    config_kwargs.update(overrides)
    config = plugin.ZuojiaRoomConfig(**config_kwargs)
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.invite_share_path: {"result": "gTwXA1qg"},
            config.home_user_list_path: {
                "result": {
                    "targetwords": 30000,
                    "targetminute": 60,
                    "spellingHomeUserInfoVos": [
                        {"cauthorid": "12345678901234567", "name": "房主甲", "userFlag": 1}
                    ],
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()
    return obj


def test_llm_first_hook_continues_and_schedules_fallback():
    obj = _make_keyword_obj(keyword_mode="llm_first", fallback_delay_seconds=3600.0)

    async def scenario():
        result = await obj.on_message_before_process(**_keyword_kwargs())
        assert result == {"action": "continue"}
        assert "qq_group_123456" in obj._pending_fallbacks
        # 清理挂起任务，避免 asyncio 警告
        await _drain_fallbacks(obj)

    asyncio.run(scenario())
    assert obj.ctx.send.texts == []


def test_tool_only_hook_continues_without_reply():
    obj = _make_keyword_obj(keyword_mode="tool_only")
    result = asyncio.run(obj.on_message_before_process(**_keyword_kwargs()))
    assert result == {"action": "continue"}
    assert obj._pending_fallbacks == {}
    assert obj.ctx.send.texts == []


def test_always_hook_aborts_and_sends():
    obj = _make_keyword_obj(keyword_mode="always")
    result = asyncio.run(obj.on_message_before_process(**_keyword_kwargs()))
    assert result == {"action": "abort"}

    assert obj.ctx.send.texts == ["qq_group_123456"]
    # 冷却期内再次命中 → 交给 LLM（continue），不再自动回复
    result2 = asyncio.run(obj.on_message_before_process(**_keyword_kwargs()))
    assert result2 == {"action": "continue"}
    assert obj.ctx.send.texts == ["qq_group_123456"]


def _keyword_kwargs_at_other():
    kwargs = _keyword_kwargs()
    kwargs["message"]["raw_message"] = [
        {"type": "at", "data": {"target_user_id": "123456"}},
        {"type": "text", "data": "有房吗"},
    ]
    kwargs["message"]["processed_plain_text"] = "有房吗"
    return kwargs


def test_always_hook_ignores_at_to_other():
    obj = _make_keyword_obj(keyword_mode="always")
    result = asyncio.run(obj.on_message_before_process(**_keyword_kwargs_at_other()))
    assert result == {"action": "continue"}
    assert obj.ctx.send.texts == []


def test_llm_first_hook_ignores_at_to_other_no_fallback():
    obj = _make_keyword_obj(keyword_mode="llm_first", fallback_delay_seconds=3600.0)
    result = asyncio.run(obj.on_message_before_process(**_keyword_kwargs_at_other()))
    assert result == {"action": "continue"}
    assert obj._pending_fallbacks == {}
    assert obj.ctx.send.texts == []


def test_always_hook_responds_at_to_other_when_filter_disabled():
    obj = _make_keyword_obj(keyword_mode="always", ignore_at_to_others=False)
    result = asyncio.run(obj.on_message_before_process(**_keyword_kwargs_at_other()))
    assert result == {"action": "abort"}
    assert obj.ctx.send.texts == ["qq_group_123456"]


def test_always_hook_respects_cooldown_seconds():
    # 关闭冷却与去重，验证 always 模式每次命中都必答
    obj = _make_keyword_obj(
        keyword_mode="always",
        keyword_cooldown_seconds=0.0,
        reply_dedup_seconds=0.0,
    )
    result = asyncio.run(obj.on_message_before_process(**_keyword_kwargs()))
    assert result == {"action": "abort"}
    result2 = asyncio.run(obj.on_message_before_process(**_keyword_kwargs()))
    assert result2 == {"action": "abort"}
    assert obj.ctx.send.texts == ["qq_group_123456", "qq_group_123456"]


def test_send_auto_reply_respects_cooldown():
    obj = _make_keyword_obj(keyword_mode="llm_first", keyword_cooldown_seconds=120.0)
    sent = asyncio.run(obj._send_auto_reply("qq_group_123456", "房间来了"))
    assert sent is True
    # 冷却期内再次发送被跳过
    sent2 = asyncio.run(obj._send_auto_reply("qq_group_123456", "房间来了"))
    assert sent2 is False
    assert obj.ctx.send.texts == ["qq_group_123456"]


def test_send_auto_reply_respects_dedup_window():
    obj = _make_keyword_obj(keyword_mode="llm_first", keyword_cooldown_seconds=0.0)
    sent = asyncio.run(obj._send_auto_reply("qq_group_123456", "房间来了"))
    assert sent is True
    # 同文本在 reply_dedup_seconds 窗口内被拦截（即使冷却关闭）
    sent2 = asyncio.run(obj._send_auto_reply("qq_group_123456", "房间来了"))
    assert sent2 is False
    assert obj.ctx.send.texts == ["qq_group_123456"]


def test_send_gated_reply_state_returns_sent_then_suppressed_on_cooldown():
    obj = _make_keyword_obj(keyword_mode="llm_first", keyword_cooldown_seconds=120.0)
    state1 = asyncio.run(obj._send_gated_reply_state("qq_group_123456", "房间来了"))
    assert state1 == "sent"
    assert obj.ctx.send.texts == ["qq_group_123456"]
    # 冷却期内再次发送被跳过
    state2 = asyncio.run(obj._send_gated_reply_state("qq_group_123456", "房间来了"))
    assert state2 == "suppressed"
    assert obj.ctx.send.texts == ["qq_group_123456"]


def test_send_gated_reply_state_returns_suppressed_on_dedup_without_cooldown():
    obj = _make_keyword_obj(keyword_mode="llm_first", keyword_cooldown_seconds=0.0)
    state1 = asyncio.run(obj._send_gated_reply_state("qq_group_123456", "房间来了"))
    assert state1 == "sent"
    # 同文本在去重窗口内被拦截（即使冷却关闭）
    state2 = asyncio.run(obj._send_gated_reply_state("qq_group_123456", "房间来了"))
    assert state2 == "suppressed"
    assert obj.ctx.send.texts == ["qq_group_123456"]


def test_send_gated_reply_state_returns_failed_when_send_raises():
    obj = _make_keyword_obj(keyword_mode="llm_first", keyword_cooldown_seconds=0.0)

    class FailingSend:
        def __init__(self):
            self.texts = []

        async def text(self, text, stream_id):
            raise RuntimeError("boom")

    obj._ctx = FakeCtx()
    obj._ctx.send = FailingSend()
    state = asyncio.run(obj._send_gated_reply_state("qq_group_123456", "房间来了"))
    assert state == "failed"
    assert obj.ctx.send.texts == []


def test_send_gated_reply_state_returns_failed_when_send_returns_false():
    obj = _make_keyword_obj(keyword_mode="llm_first", keyword_cooldown_seconds=0.0)

    class FalseSend:
        def __init__(self):
            self.texts = []

        async def text(self, text, stream_id):
            self.texts.append(stream_id)
            return False

    obj._ctx = FakeCtx()
    obj._ctx.send = FalseSend()
    state = asyncio.run(obj._send_gated_reply_state("qq_group_123456", "房间来了"))
    assert state == "failed"
    assert obj.ctx.send.texts == ["qq_group_123456"]


def test_send_gated_reply_state_empty_stream_is_failed():
    obj = _make_keyword_obj(keyword_mode="llm_first", keyword_cooldown_seconds=0.0)
    state = asyncio.run(obj._send_gated_reply_state("", "房间来了"))
    assert state == "failed"
    assert obj.ctx.send.texts == []


def test_before_send_dedup_blocks_same_text_within_window():
    obj = _make_keyword_obj(keyword_mode="always")
    obj._mark_reply_sent("qq_group_123456", "房间来了")
    msg = {
        "session_id": "qq_group_123456",
        "processed_plain_text": "房间来了",
    }
    result = asyncio.run(obj.block_duplicate_reply(message=msg))
    assert result == {"action": "abort"}
    # 不同文本放行
    msg2 = {"session_id": "qq_group_123456", "processed_plain_text": "别的话题"}
    assert asyncio.run(obj.block_duplicate_reply(message=msg2)) is None
    # 图片/表情放行
    assert asyncio.run(obj.block_duplicate_reply(message={"session_id": "qq_group_123456", "is_emoji": True})) is None


def test_before_send_records_outbound_for_watchdog():
    obj = _make_keyword_obj(keyword_mode="llm_first")
    assert asyncio.run(obj.block_duplicate_reply(
        message={"session_id": "qq_group_123456", "processed_plain_text": "模型回了一句"}
    )) is None
    assert obj._last_outbound_sent.get("qq_group_123456", 0.0) > 0


def test_fallback_fires_when_llm_silent():
    obj = _make_keyword_obj(keyword_mode="llm_first", fallback_delay_seconds=0.0)
    # armed_at 取正值，且 LLM 无出站（_last_outbound_sent 缺省 0.0 < 1.0）→ 触发兜底
    asyncio.run(obj._fallback_after("qq_group_123456", "10001", "123456", "来个码字房", armed_at=1.0))
    assert obj.ctx.send.texts == ["qq_group_123456"]


def test_fallback_cancelled_when_llm_already_replied():
    obj = _make_keyword_obj(keyword_mode="llm_first", fallback_delay_seconds=0.0)
    # LLM 在 armed_at 之后发过出站 → 兜底放弃
    obj._last_outbound_sent["qq_group_123456"] = 10.0
    asyncio.run(obj._fallback_after("qq_group_123456", "10001", "123456", "来个码字房", armed_at=1.0))
    assert obj.ctx.send.texts == []


def test_fallback_respects_cooldown():
    obj = _make_keyword_obj(keyword_mode="llm_first", fallback_delay_seconds=0.0, keyword_cooldown_seconds=120.0)
    # 先自动回复一次，进入冷却
    asyncio.run(obj._send_auto_reply("qq_group_123456", "上一次回复"))
    # 再走兜底 → 冷却期内不发
    asyncio.run(obj._fallback_after("qq_group_123456", "10001", "123456", "来个码字房", armed_at=1.0))
    assert obj.ctx.send.texts == ["qq_group_123456"]


def test_build_keyword_reply_success_returns_room_text():
    obj = _make_keyword_obj(keyword_mode="llm_first")
    reply = asyncio.run(obj._build_keyword_reply("qq_group_123456", "123456", "10001", "来个码字房"))
    assert "邀请你加入" in reply
    assert "spellInvite" in reply


def test_build_keyword_reply_failure_group_member_returns_friendly():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
        group_whitelist=["123456"],
        trigger_modes=["keyword"],
        keyword_reply_mode="llm_first",
        keyword_must_reply=True,
        keyword_group_failure_reply="现在没有房哦",
        keyword_failure_llm_reply=False,
    )
    client = FakeClient(config, {config.current_room_path: {"result": {"homeId": 0}}})
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config


async def _drain_fallbacks(obj):
    await obj._cancel_all_fallbacks()


def test_room_data_creates_and_sends_link_after_window_expired():
    config = plugin.ZuojiaRoomConfig(admin_user_ids=["10001"], room_data_create_if_missing=True,
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 0}},
            config.create_room_path: {"result": {"homeid": 19693123}},
            config.invite_share_path: {"result": "new-share"},
            config.home_user_list_path: {"result": {"spellingHomeUserInfoVos": []}},
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()
    # 窗口已过期：默认 120s，这里设为 121s 前
    obj._last_room_link_sent["chat-real-1"] = time.monotonic() - 121.0

    result = asyncio.run(obj.get_room_data(stream_id="chat-real-1", user_id="10001"))
    assert result["success"] is True
    assert result["created"] is True
    assert result["content"] == plugin.TOOL_ALREADY_SENT_ROOM_LINK
    assert obj.ctx.send.texts == ["chat-real-1"]


def test_room_data_window_zero_disables_suppression():
    config = plugin.ZuojiaRoomConfig(admin_user_ids=["10001"], room_data_create_if_missing=True,
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
        room_data_suppress_after_link_seconds=0.0,
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 0}},
            config.create_room_path: {"result": {"homeid": 19693123}},
            config.invite_share_path: {"result": "new-share"},
            config.home_user_list_path: {"result": {"spellingHomeUserInfoVos": []}},
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()
    obj._last_room_link_sent["chat-real-1"] = time.monotonic()

    result = asyncio.run(obj.get_room_data(stream_id="chat-real-1", user_id="10001"))
    assert result["success"] is True
    assert result["created"] is True
    assert result["content"] == plugin.TOOL_ALREADY_SENT_ROOM_LINK
    assert obj.ctx.send.texts == ["chat-real-1"]


def test_room_data_suppression_is_per_stream():
    config = plugin.ZuojiaRoomConfig(admin_user_ids=["10001"], room_data_create_if_missing=True,
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 0}},
            config.create_room_path: {"result": {"homeid": 19693123}},
            config.invite_share_path: {"result": "new-share"},
            config.home_user_list_path: {"result": {"spellingHomeUserInfoVos": []}},
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()
    # 只在 chat-real-1 标记；chat-real-2 不受影响
    obj._last_room_link_sent["chat-real-1"] = time.monotonic()

    result = asyncio.run(obj.get_room_data(stream_id="chat-real-2", user_id="10001"))
    assert result["success"] is True
    assert result["created"] is True
    assert result["content"] == plugin.TOOL_ALREADY_SENT_ROOM_LINK
    assert obj.ctx.send.texts == ["chat-real-2"]


def test_room_link_sent_records_mark_only_when_sent():
    config = plugin.ZuojiaRoomConfig(
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 19693024}},
            config.invite_share_path: {"result": "gTwXA1qg"},
            config.home_user_list_path: {
                "result": {
                    "targetwords": 30000,
                    "targetminute": 60,
                    "spellingHomeUserInfoVos": [
                        {"cauthorid": "12345678901234567", "name": "房主甲", "userFlag": 1}
                    ]
                }
            },
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    calls = []
    original_mark = obj._mark_room_link_sent
    obj._mark_room_link_sent = lambda sid: (calls.append(sid), original_mark(sid))[1]

    # 第一次成功发送：应记录
    result1 = asyncio.run(obj.get_room_link(stream_id="chat-real-1", user_id="10001"))
    assert result1["success"] is True
    assert calls == ["chat-real-1"]

    # 第二次被冷却抑制（suppressed）：不应再次记录
    result2 = asyncio.run(obj.get_room_link(stream_id="chat-real-1", user_id="10001"))
    assert result2["success"] is True
    assert result2["content"] == plugin.TOOL_ALREADY_SENT_ROOM_LINK
    assert calls == ["chat-real-1"]


def test_room_data_created_send_also_marks_link():
    config = plugin.ZuojiaRoomConfig(admin_user_ids=["10001"], room_data_create_if_missing=True,
        api_base_url="https://pcwrite.yuewen.com",
        author_id="12345678901234567",
    )
    client = FakeClient(
        config,
        {
            config.current_room_path: {"result": {"homeId": 0}},
            config.create_room_path: {"result": {"homeid": 19693123}},
            config.invite_share_path: {"result": "new-share"},
            config.home_user_list_path: {"result": {"spellingHomeUserInfoVos": []}},
        },
    )
    obj = plugin.ZuojiaRoomPlugin()
    obj._plugin_config_instance = config
    obj.client = client
    obj._ctx = FakeCtx()

    result = asyncio.run(obj.get_room_data(stream_id="chat-real-1", user_id="10001"))
    assert result["success"] is True
    assert result["created"] is True
    assert obj._last_room_link_sent["chat-real-1"] > 0

def test_review_failed_send_does_not_start_cooldown_or_abort():
    obj = _make_keyword_obj()
    async def failed(*args, **kwargs):
        return False
    obj.ctx.send.text = failed
    assert asyncio.run(obj.on_message_before_process(**_keyword_kwargs())) == {"action": "continue"}
    assert not obj._in_keyword_cooldown("qq_group_123456")

def test_review_fresh_boot_has_no_phantom_cooldown(monkeypatch):
    obj = _make_keyword_obj()
    monkeypatch.setattr(plugin.time, "monotonic", lambda: 1.0)
    assert not obj._in_keyword_cooldown("fresh")

def test_review_negative_dedup_age_is_not_duplicate():
    assert not plugin.should_dedupe_outbound_repeat("same", "same", 9000, 120, 1)

def test_review_unexpected_room_error_is_not_no_room():
    obj = _make_keyword_obj()
    async def broken(**kwargs):
        raise plugin.RoomShareError("缺少 author_id，请配置作家助手作者 ID")
    obj.client.get_room = broken
    assert asyncio.run(obj._safe_get_room(False, False))["reply"] != obj.config.no_room_message

def test_review_fresh_boot_has_no_phantom_room_link(monkeypatch):
    obj = _make_keyword_obj()
    monkeypatch.setattr(plugin.time, "monotonic", lambda: 1.0)
    assert not obj._room_link_sent_within_window("fresh")

def test_review_generic_room_errors_are_not_no_room():
    obj = _make_keyword_obj()
    async def broken(**kwargs):
        raise KeyError("unknown-template-field")
    obj.client.get_room = broken
    obj.client.get_room_data = broken
    assert asyncio.run(obj._safe_get_room(False, False))["reply"] != obj.config.no_room_message
    assert asyncio.run(obj._safe_get_room_data(False, False))["reply"] != obj.config.room_data_no_room_message

def test_bb_creation_policy_separates_chat_members_and_login_admins():
    config = plugin.ZuojiaRoomConfig(group_whitelist=["fixture-group"])
    assert plugin.can_create_room("fixture-group", "member", config)
    assert not plugin.can_create_room("blocked", "member", config)
    assert not plugin.can_create_room("fixture-group", "", config)
    assert not plugin.is_login_user("member", config)
    restricted = plugin.ZuojiaRoomConfig(group_whitelist=["fixture-group"], room_creation_permission="admins", admin_user_ids=["owner"])
    assert not plugin.can_create_room("fixture-group", "member", restricted)
    assert plugin.can_create_room("fixture-group", "owner", restricted)

def test_bb_member_tools_can_create_without_login_admin_permission():
    for method_name, switch in (("get_room_link", "create_room_if_missing"), ("get_room_data", "room_data_create_if_missing")):
        obj = plugin.create_plugin()
        obj._ctx = FakeCtx()
        obj._plugin_config_instance = plugin.ZuojiaRoomConfig(group_whitelist=["fixture-group"])
        calls = []
        async def room(**kwargs):
            calls.append(kwargs)
            return {"success": True, "created": True, "room": {"room_number": "fixture"}, "reply": "fixture invitation"}
        obj._safe_get_room = obj._safe_get_room_data = room
        asyncio.run(getattr(obj, method_name)(stream_id="fixture-stream", group_id="fixture-group", user_id="member"))
        assert calls == [{"auto_create": True, "allowed_to_create": True}]
        setattr(obj.config, switch, False)
        asyncio.run(getattr(obj, method_name)(stream_id="another-stream", group_id="fixture-group", user_id="member"))
        assert calls[-1]["auto_create"] is False

def test_bb_login_aliases_only_present_terms_before_confirmation():
    obj = plugin.create_plugin()
    obj._ctx = FakeCtx()
    obj._plugin_config_instance = plugin.ZuojiaRoomConfig(admin_user_ids=["owner"])
    for command in ("/拼字登录",):
        result = asyncio.run(obj.handle_login(stream_id="fixture", user_id="owner", raw_message=command))
        assert "三份官方协议" in result[1]
        assert obj._login_task is None
    assert plugin.ZuojiaRoomConfig().keywords == ["有房吗", "码字房", "拼字房"]
