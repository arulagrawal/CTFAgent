import asyncio
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock
import pytest
from fastapi.testclient import TestClient
from backend.llm_models import validate_settings, estimate_response, MODEL_REGISTRY
from backend.openai_gateway import (
    structured_response,
    require_parsed,
    ModelResponseError,
)
from backend.session import Session
from backend.ai_agent import SingleCommand, CTFAgent
from backend import main


@pytest.mark.parametrize("model", MODEL_REGISTRY)
def test_model_defaults(model):
    assert validate_settings(model, "default") == (model, None)
    for effort in MODEL_REGISTRY[model].reasoning_efforts:
        assert validate_settings(model, effort) == (model, effort)


def test_custom_and_invalid_effort():
    assert validate_settings("custom-new-model", None) == ("custom-new-model", None)
    with pytest.raises(ValueError):
        validate_settings("gpt-6-astra", "none")
    with pytest.raises(ValueError):
        validate_settings("bad model", None)


async def test_request_parameters_and_continuation():
    client = Mock()
    client.with_options.return_value.responses.parse = AsyncMock(return_value="result")
    await structured_response(
        client,
        model="gpt-6-luna",
        schema=SingleCommand,
        instructions="test",
        input="test",
        previous_id="prior",
    )
    args = client.with_options.return_value.responses.parse.call_args.kwargs
    assert (
        "reasoning" not in args
        and args["previous_response_id"] == "prior"
        and args["service_tier"] == "default"
    )
    await structured_response(
        client,
        model="gpt-6-astra",
        schema=SingleCommand,
        instructions="test",
        input="test",
        effort="max",
        tier="flex",
    )
    assert client.with_options.return_value.responses.parse.call_args.kwargs[
        "reasoning"
    ] == {"effort": "max"}


async def test_request_deadline():
    client = Mock()

    async def slow(**kwargs):
        await asyncio.sleep(1)

    client.with_options.return_value.responses.parse = slow
    with pytest.raises(TimeoutError):
        await structured_response(
            client,
            model="custom",
            schema=SingleCommand,
            instructions="test",
            input="test",
            timeout=0.01,
        )


@pytest.mark.parametrize(
    "response",
    [
        NS(status="incomplete", incomplete_details=NS(reason="max_output_tokens")),
        NS(status="completed", output=[NS(content=[NS(type="refusal", refusal="No")])]),
        NS(status="completed", output=[], output_parsed=None),
    ],
)
def test_bad_responses(response):
    with pytest.raises(ModelResponseError):
        require_parsed(response)


def test_usage_and_pricing():
    response = NS(
        service_tier="flex",
        usage=NS(
            input_tokens=1000,
            output_tokens=100,
            total_tokens=1100,
            input_tokens_details=NS(cached_tokens=500),
        ),
    )
    assert estimate_response("gpt-6-luna", response) == pytest.approx(0.0000525)
    assert estimate_response("custom", response) is None
    state = Session("s", "gpt-6-luna", "gpt-6-luna")
    state.reset_usage()
    state.update_usage_from_response(response)
    state.update_usage_from_response(response)
    assert state.usage["requests"] == 2 and state.usage[
        "estimated_cost_usd"
    ] == pytest.approx(0.000105)


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "ai_agent", Mock())
    main.sessions.clear()
    main.background_tasks.clear()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "solve_challenge_background_form", AsyncMock())
    # Deliberately omit lifespan so unit tests never contact Docker/OpenAI.
    return TestClient(main.app)


@pytest.mark.parametrize(
    "data,files",
    [
        ({"url": "https://example.com"}, None),
        ({"host": "localhost", "port": "1337"}, None),
        ({}, {"handouts": ("note.txt", b"example")}),
    ],
)
def test_submission(api, data, files):
    response = api.post(
        "/challenges/start", data={**data, "model": "custom-model"}, files=files
    )
    assert response.status_code == 200, response.text
    session = api.get("/challenges/" + response.json()["session_id"]).json()
    assert session["model"] == "custom-model" and session["reasoning_effort"] is None


@pytest.mark.parametrize(
    "data",
    [
        {"url": "file:///etc/passwd"},
        {"host": "localhost", "port": "70000"},
        {
            "url": "https://example.com",
            "model": "gpt-6-astra",
            "reasoning_effort": "none",
        },
    ],
)
def test_invalid_submission(api, data):
    assert api.post("/challenges/start", data=data).status_code == 400


def test_unsafe_upload(api):
    assert (
        api.post(
            "/challenges/start", files={"handouts": ("../escape", b"bad")}
        ).status_code
        == 400
    )


def test_catalog(api):
    result = api.get("/models").json()
    assert len(result["models"]) == 8 and result["default_reasoning_effort"] is None


async def test_multiple_viewers():
    manager = main.WebSocketManager()
    a = AsyncMock()
    b = AsyncMock()
    await manager.connect(a, "s")
    await manager.connect(b, "s")
    manager.disconnect("s", a)
    await manager.send_message(
        "s", main.WebSocketMessage(type="status", session_id="s", data={})
    )
    b.send_text.assert_awaited_once()
    a.send_text.assert_not_awaited()


async def test_agent_error_stops_and_clears_thinking():
    agent = object.__new__(CTFAgent)
    agent.sessions = {"s": Session("s", "gpt-6-luna", "gpt-6-luna")}
    agent.service_tier = "default"
    agent.client = Mock()
    agent.client.with_options.return_value.responses.parse = AsyncMock(
        side_effect=RuntimeError("inaccessible model")
    )
    agent.docker_manager = Mock()
    agent.docker_manager.execute_command_stream = AsyncMock()
    callback = AsyncMock()
    result = await agent._execute_with_ai("s", "test", [], callback)
    assert result[0].action_type == "ai_error"
    assert callback.call_args_list[-1].args == ("ai_status", {"thinking": False})
    agent.docker_manager.execute_command_stream.assert_not_called()


async def test_writeup_selected_model():
    agent = object.__new__(CTFAgent)
    agent.default_model = "gpt-6-luna"
    agent.service_tier = "default"
    agent.client = Mock()
    response = NS(
        status="completed",
        output=[],
        output_parsed=NS(writeup="done", solve_script=None),
    )
    agent.client.with_options.return_value.responses.parse = AsyncMock(
        return_value=response
    )
    result = await agent.generate_writeup(
        "s",
        "name",
        "misc",
        "flag{ok}",
        [],
        model="gpt-6-astra",
        reasoning_effort="high",
    )
    assert result.writeup == "done"
    assert (
        agent.client.with_options.return_value.responses.parse.call_args.kwargs["model"]
        == "gpt-6-astra"
    )


async def test_flag_split_across_chunks():
    agent = object.__new__(CTFAgent)
    agent.sessions = {"s": Session("s", "gpt-6-luna", "gpt-6-luna")}
    agent.service_tier = "default"
    agent.client = Mock()
    agent.docker_manager = Mock()
    response = NS(
        id="response",
        status="completed",
        output=[],
        output_parsed=SingleCommand(command="cat flag", reasoning="Inspect handout"),
        usage=None,
    )
    agent.client.with_options.return_value.responses.parse = AsyncMock(
        return_value=response
    )

    async def execute(s, command, callback, **kwargs):
        await callback("stdout", "flag{split_")
        await callback("stdout", "value}")
        assert kwargs["stop_event"].is_set()
        return {
            "success": False,
            "exit_code": -1,
            "stdout": "flag{split_value}",
            "stderr": "",
        }

    agent.docker_manager.execute_command_stream = execute
    result = await agent._execute_with_ai("s", "test", [], AsyncMock())
    assert len([a for a in result if a.action_type == "flag_found"]) == 1
    assert result[0].success


@pytest.mark.parametrize("mode", ["timeout", "flag", "cancel"])
async def test_docker_termination_and_callback_drain(mode):
    import threading
    from backend.docker_manager import DockerManager

    manager = object.__new__(DockerManager)
    manager.client = Mock()
    container = Mock()
    container.id = "container"
    manager.containers = {"s": container}
    killed = threading.Event()
    container.kill.side_effect = killed.set
    manager.client.api.exec_create.return_value = {"Id": "exec"}
    manager.client.api.exec_inspect.return_value = {"ExitCode": 137}

    def stream(*args, **kwargs):
        yield (b"first", None)
        killed.wait(2)
        yield (b"last", None)

    manager.client.api.exec_start.side_effect = stream
    stop = asyncio.Event()
    chunks = []

    async def callback(channel, text):
        chunks.append(text)
        if mode == "flag":
            stop.set()

    task = asyncio.create_task(
        manager.execute_command_stream(
            "s",
            "sleep 100",
            callback,
            timeout=0.05 if mode == "timeout" else 1,
            stop_event=stop,
        )
    )
    if mode == "cancel":
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await task
        assert result["timed_out"] == (mode == "timeout")
    assert killed.is_set() and chunks == ["first", "last"]
