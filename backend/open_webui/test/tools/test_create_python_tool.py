import json
import pytest

from open_webui.test_support import AbstractPostgresTest, mock_webui_user
from open_webui.models.tools import Tools as ToolsModel
from open_webui.tools.builtin import create_python_tool


VALID_TOOL_CODE = '''
class Tools:
    """A simple calculator tool."""

    def add(self, a: int, b: int) -> str:
        """
        Add two numbers together.

        :param a: First number
        :param b: Second number
        :return: The sum as a string
        """
        return str(a + b)
'''

VALID_TOOL_CODE_WITH_IMPORT = '''
import math

class Tools:
    """A math helper tool."""

    def sqrt(self, n: float) -> str:
        """
        Calculate the square root of a number.

        :param n: The number
        :return: The square root as a string
        """
        return str(math.sqrt(n))
'''

INVALID_SYNTAX_CODE = '''
class Tools:
    def add(self, a, b)
        return a + b
'''

NO_TOOLS_CLASS_CODE = '''
class Helper:
    def add(self, a: int, b: int) -> str:
        return str(a + b)
'''

PASSING_TEST_CODE = '''
tool = Tools()
assert tool.add(2, 3) == "5"
assert tool.add(0, 0) == "0"
'''

FAILING_TEST_CODE = '''
tool = Tools()
assert tool.add(2, 3) == "6", "Expected 6 but got something else"
'''


class TestCreatePythonTool(AbstractPostgresTest):

    def _make_user_dict(self, user):
        return {"id": user.id, "role": user.role, "name": user.name}

    def _make_request_with_tools(self):
        request = self.make_request("/api/v1/tools/create", method="POST")
        if not hasattr(request.app.state, "TOOLS"):
            request.app.state.TOOLS = {}
        return request

    def test_create_tool_valid_code(self):
        with mock_webui_user(id="test_user_1", role="admin") as user:
            request = self._make_request_with_tools()
            result = self.run_async(
                create_python_tool(
                    tool_name="Simple Calculator",
                    tool_id="simple_calc",
                    description="A simple calculator",
                    code=VALID_TOOL_CODE,
                    __request__=request,
                    __user__=self._make_user_dict(user),
                    __event_emitter__=None,
                )
            )
            data = json.loads(result)
            assert data["status"] == "success", f"Expected success but got: {data}"
            assert data["tool_id"] == "simple_calc"
            assert data["name"] == "Simple Calculator"
            assert len(data["specs"]) > 0

            # Verify persisted in DB
            db_tool = ToolsModel.get_tool_by_id("simple_calc")
            assert db_tool is not None
            assert db_tool.name == "Simple Calculator"

            # Verify cached in app state
            assert "simple_calc" in request.app.state.TOOLS

    def test_create_tool_invalid_syntax(self):
        with mock_webui_user(id="test_user_2", role="admin") as user:
            request = self._make_request_with_tools()
            result = self.run_async(
                create_python_tool(
                    tool_name="Bad Tool",
                    tool_id="bad_tool",
                    description="Should fail",
                    code=INVALID_SYNTAX_CODE,
                    __request__=request,
                    __user__=self._make_user_dict(user),
                    __event_emitter__=None,
                )
            )
            data = json.loads(result)
            assert data["status"] == "error"
            assert "validation failed" in data["error"].lower() or "syntax" in data["error"].lower()

    def test_create_tool_missing_tools_class(self):
        with mock_webui_user(id="test_user_3", role="admin") as user:
            request = self._make_request_with_tools()
            result = self.run_async(
                create_python_tool(
                    tool_name="No Tools Class",
                    tool_id="no_tools",
                    description="Should fail",
                    code=NO_TOOLS_CLASS_CODE,
                    __request__=request,
                    __user__=self._make_user_dict(user),
                    __event_emitter__=None,
                )
            )
            data = json.loads(result)
            assert data["status"] == "error"
            assert "Tools" in data["error"] or "class" in data["error"].lower()

    def test_create_tool_with_passing_tests(self):
        with mock_webui_user(id="test_user_4", role="admin") as user:
            request = self._make_request_with_tools()
            result = self.run_async(
                create_python_tool(
                    tool_name="Tested Calculator",
                    tool_id="tested_calc",
                    description="A tested calculator",
                    code=VALID_TOOL_CODE,
                    test_code=PASSING_TEST_CODE,
                    __request__=request,
                    __user__=self._make_user_dict(user),
                    __event_emitter__=None,
                )
            )
            data = json.loads(result)
            assert data["status"] == "success", f"Expected success but got: {data}"

    def test_create_tool_with_failing_tests(self):
        with mock_webui_user(id="test_user_5", role="admin") as user:
            request = self._make_request_with_tools()
            result = self.run_async(
                create_python_tool(
                    tool_name="Failing Test Tool",
                    tool_id="fail_test",
                    description="Should fail tests",
                    code=VALID_TOOL_CODE,
                    test_code=FAILING_TEST_CODE,
                    __request__=request,
                    __user__=self._make_user_dict(user),
                    __event_emitter__=None,
                )
            )
            data = json.loads(result)
            assert data["status"] == "error"
            assert "test" in data["error"].lower() or "assert" in data["error"].lower()

    def test_create_tool_duplicate_id(self):
        with mock_webui_user(id="test_user_6", role="admin") as user:
            request = self._make_request_with_tools()
            # Create first tool
            self.run_async(
                create_python_tool(
                    tool_name="First Tool",
                    tool_id="dup_tool",
                    description="First",
                    code=VALID_TOOL_CODE,
                    __request__=request,
                    __user__=self._make_user_dict(user),
                    __event_emitter__=None,
                )
            )
            # Try creating with same ID
            result = self.run_async(
                create_python_tool(
                    tool_name="Second Tool",
                    tool_id="dup_tool",
                    description="Second",
                    code=VALID_TOOL_CODE,
                    __request__=request,
                    __user__=self._make_user_dict(user),
                    __event_emitter__=None,
                )
            )
            data = json.loads(result)
            assert "error" in data
            assert "already exists" in data["error"]

    def test_create_tool_invalid_id(self):
        with mock_webui_user(id="test_user_7", role="admin") as user:
            request = self._make_request_with_tools()
            result = self.run_async(
                create_python_tool(
                    tool_name="Bad ID Tool",
                    tool_id="bad id!",
                    description="Should fail",
                    code=VALID_TOOL_CODE,
                    __request__=request,
                    __user__=self._make_user_dict(user),
                    __event_emitter__=None,
                )
            )
            data = json.loads(result)
            assert "error" in data
            assert "Invalid tool_id" in data["error"]

    def test_create_tool_no_request(self):
        result = self.run_async(
            create_python_tool(
                tool_name="No Request",
                tool_id="no_req",
                description="Should fail",
                code=VALID_TOOL_CODE,
                __request__=None,
                __user__={"id": "1"},
                __event_emitter__=None,
            )
        )
        data = json.loads(result)
        assert "error" in data

    def test_create_tool_no_user(self):
        request = self._make_request_with_tools()
        result = self.run_async(
            create_python_tool(
                tool_name="No User",
                tool_id="no_user",
                description="Should fail",
                code=VALID_TOOL_CODE,
                __request__=request,
                __user__={},
                __event_emitter__=None,
            )
        )
        data = json.loads(result)
        assert "error" in data
