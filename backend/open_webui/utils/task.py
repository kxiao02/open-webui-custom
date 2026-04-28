import logging
import math
import re
from datetime import datetime
from typing import Optional, Any
import uuid


from open_webui.utils.misc import get_last_user_message, get_messages_content

from open_webui.config import DEFAULT_RAG_TEMPLATE

log = logging.getLogger(__name__)


_FOLLOW_UP_USER_MARKERS = (
    "这",
    "那",
    "还",
    "再",
    "刚才",
    "前面",
    "上面",
    "这种情况",
    "这样的话",
    "那我",
    "那如果",
    "是否",
    "会不会",
    "would this",
    "does that",
    "what about",
    "how about",
    "in that case",
    "then ",
)
_COMMON_CJK_FACT_TOKENS = {
    "我的",
    "我们",
    "你们",
    "请问",
    "一下",
    "这个",
    "那个",
    "如果",
    "还有",
    "是不是",
    "是否",
    "一个",
    "个月",
    "今天",
    "今年",
}
_SESSION_USER_FACTS_LIMIT = 12
_SESSION_USER_FACTS_PROMPT_LIMIT = 5
_GENERIC_DETAIL_PATTERNS = (
    re.compile(r"\d"),
    re.compile(
        r"(\b\d{4}[-/]\d{1,2}[-/]\d{1,2}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b|"
        r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b|"
        r"(年|月|日|周|天|小时|分鐘|分钟|秒))",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"(%|[$¥€£]|usd|eur|cny|rmb|人民币|元|块|公里|km|kg|mb|gb|tb|hz|°c|°f)",
        flags=re.IGNORECASE,
    ),
    re.compile(
        r"([a-z0-9]+(?:[-_/.:#][a-z0-9]+)+|\bv?\d+\.\d+(?:\.\d+)*\b)",
        flags=re.IGNORECASE,
    ),
    re.compile(r"([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}|https?://\S+|www\.\S+)", flags=re.IGNORECASE),
    re.compile(r"(`[^`]+`|\"[^\"]{3,}\"|'[^']{3,}'|“[^”]{2,}”|‘[^’]{2,}’)"),
)


def _extract_text_from_message_content(content: Any) -> str:
    if isinstance(content, str):
        return re.sub(r"\s+", " ", content).strip()

    if not isinstance(content, list):
        return ""

    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") not in {"text", "input_text", "output_text"}:
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            parts.append(re.sub(r"\s+", " ", text).strip())

    return "\n".join(parts).strip()


def _tokenize_relevant_fact_text(text: str) -> set[str]:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return set()

    tokens: set[str] = set()
    lowered = normalized.lower()

    for match in re.finditer(r"[a-z0-9_]{2,}", lowered):
        tokens.add(match.group(0))

    for match in re.finditer(r"\d+(?:\.\d+)?%?", lowered):
        tokens.add(match.group(0))

    for segment in re.findall(r"[\u4e00-\u9fff]{2,}", normalized):
        if len(segment) <= 4 and segment not in _COMMON_CJK_FACT_TOKENS:
            tokens.add(segment)

        for size in (2, 3):
            if len(segment) < size:
                continue
            for index in range(len(segment) - size + 1):
                token = segment[index : index + size]
                if token in _COMMON_CJK_FACT_TOKENS:
                    continue
                tokens.add(token)

    return tokens


def _looks_like_follow_up_user_turn(text: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip().lower()
    if not normalized:
        return False

    if any(marker in normalized for marker in _FOLLOW_UP_USER_MARKERS):
        return True

    if len(normalized) <= 32 and normalized.endswith(("吗", "么", "呢", "?")):
        return True

    return False


def _score_salient_detail_markers(text: str) -> int:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip()
    if not normalized:
        return 0

    score = sum(1 for pattern in _GENERIC_DETAIL_PATTERNS if pattern.search(normalized))

    # Long, information-dense turns often carry concrete state even when they
    # lack an obvious numeric or identifier pattern.
    if len(normalized) >= 48:
        score += 1

    return score


def _normalize_session_fact_text(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def normalize_session_user_facts(facts: Any) -> list[dict[str, str]]:
    if not isinstance(facts, list):
        return []

    normalized_facts: list[dict[str, str]] = []
    seen_texts: set[str] = set()
    for fact in facts:
        if not isinstance(fact, dict):
            continue

        text = _normalize_session_fact_text(fact.get("text"))
        if not text or text in seen_texts:
            continue

        confidence = str(fact.get("confidence") or "medium").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"

        normalized_facts.append({"text": text, "confidence": confidence})
        seen_texts.add(text)

    return normalized_facts


def extract_session_user_facts(
    messages: Optional[list[dict]], limit: int = _SESSION_USER_FACTS_LIMIT
) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        return []

    scored_facts: list[tuple[int, int, dict[str, str]]] = []
    for index, message in enumerate(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue

        text = _extract_text_from_message_content(message.get("content"))
        if not text:
            continue

        detail_score = _score_salient_detail_markers(text)
        token_count = len(_tokenize_relevant_fact_text(text))
        if detail_score < 2 and not (detail_score >= 1 and token_count >= 6):
            if len(text) < 80 or token_count < 8:
                continue

        score = min(detail_score, 4) + min(token_count, 4)
        confidence = "high" if score >= 6 else "medium"
        scored_facts.append((index, score, {"text": text, "confidence": confidence}))

    seen_texts: set[str] = set()
    ordered_facts: list[dict[str, str]] = []
    for _, _, fact in sorted(scored_facts, key=lambda item: (-item[0], -item[1])):
        text = fact["text"]
        if text in seen_texts:
            continue
        seen_texts.add(text)
        ordered_facts.append(fact)
        if len(ordered_facts) >= limit:
            break

    return ordered_facts


def _score_session_fact_relevance(
    candidate_text: str,
    latest_user_turn: str,
    latest_tokens: set[str],
    latest_is_follow_up: bool,
    immediate_previous_text: str = "",
) -> int:
    candidate_tokens = _tokenize_relevant_fact_text(candidate_text)
    shared_tokens = latest_tokens & candidate_tokens
    score = len(shared_tokens) * 3
    salient_detail_score = _score_salient_detail_markers(candidate_text)

    if candidate_text == immediate_previous_text:
        score += 2
        if latest_is_follow_up:
            score += 3

    score += min(salient_detail_score, 3)
    if shared_tokens and salient_detail_score > 0:
        score += 1

    return score


def get_relevant_prior_user_facts(
    messages: Optional[list[dict]], limit: int = 3
) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        return []

    user_turns: list[str] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _extract_text_from_message_content(message.get("content"))
        if text:
            user_turns.append(text)

    if len(user_turns) < 2:
        return []

    latest_user_turn = user_turns[-1]
    latest_tokens = _tokenize_relevant_fact_text(latest_user_turn)
    latest_is_follow_up = _looks_like_follow_up_user_turn(latest_user_turn)

    scored_facts: list[tuple[int, int, dict[str, str]]] = []
    immediate_previous_text = user_turns[-2] if len(user_turns) >= 2 else ""

    for index, candidate_text in enumerate(user_turns[:-1]):
        if candidate_text == latest_user_turn:
            continue

        score = _score_session_fact_relevance(
            candidate_text,
            latest_user_turn,
            latest_tokens,
            latest_is_follow_up,
            immediate_previous_text=immediate_previous_text,
        )

        if score < 3:
            continue

        confidence = "high" if score >= 8 else "medium"
        scored_facts.append(
            (
                score,
                index,
                {
                    "confidence": confidence,
                    "text": candidate_text,
                },
            )
        )

    if not scored_facts and latest_is_follow_up:
        fallback_text = immediate_previous_text
        if fallback_text:
            return [{"confidence": "medium", "text": fallback_text}]

    seen_texts: set[str] = set()
    ordered_facts: list[dict[str, str]] = []
    for _, _, fact in sorted(scored_facts, key=lambda item: (-item[0], -item[1])):
        text = fact["text"]
        if text in seen_texts:
            continue
        seen_texts.add(text)
        ordered_facts.append(fact)
        if len(ordered_facts) >= limit:
            break

    return ordered_facts


def get_relevant_session_user_facts(
    messages: Optional[list[dict]],
    stored_facts: Optional[list[dict[str, str]]] = None,
    limit: int = _SESSION_USER_FACTS_PROMPT_LIMIT,
) -> list[dict[str, str]]:
    if not isinstance(messages, list):
        return []

    latest_user_turn = get_last_user_message(messages)
    if not latest_user_turn:
        return []

    latest_user_turn = _normalize_session_fact_text(latest_user_turn)
    latest_tokens = _tokenize_relevant_fact_text(latest_user_turn)
    latest_is_follow_up = _looks_like_follow_up_user_turn(latest_user_turn)

    user_turns = [
        _extract_text_from_message_content(message.get("content"))
        for message in messages
        if isinstance(message, dict) and message.get("role") == "user"
    ]
    user_turns = [turn for turn in user_turns if turn]
    immediate_previous_text = user_turns[-2] if len(user_turns) >= 2 else ""

    candidate_facts = normalize_session_user_facts(
        stored_facts if stored_facts is not None else extract_session_user_facts(messages)
    )
    if not candidate_facts:
        return []

    scored_facts: list[tuple[int, int, dict[str, str]]] = []
    for index, fact in enumerate(candidate_facts):
        candidate_text = fact["text"]
        if candidate_text == latest_user_turn:
            continue

        score = _score_session_fact_relevance(
            candidate_text,
            latest_user_turn,
            latest_tokens,
            latest_is_follow_up,
            immediate_previous_text=immediate_previous_text,
        )
        if score < 3:
            continue

        scored_facts.append((score, index, fact))

    relevant_facts: list[dict[str, str]] = []
    seen_texts: set[str] = set()
    for _, _, fact in sorted(scored_facts, key=lambda item: (-item[0], item[1])):
        text = fact["text"]
        if text in seen_texts:
            continue
        seen_texts.add(text)
        relevant_facts.append(fact)
        if len(relevant_facts) >= limit:
            break

    if relevant_facts:
        return relevant_facts

    if latest_is_follow_up:
        fallback_facts = []
        for fact in candidate_facts:
            text = fact["text"]
            if text == latest_user_turn or text in seen_texts:
                continue
            fallback_facts.append(fact)
            if len(fallback_facts) >= min(limit, 2):
                break
        return fallback_facts

    return []


def build_relevant_prior_user_facts_block(
    messages: Optional[list[dict]], limit: int = 3
) -> str:
    facts = get_relevant_prior_user_facts(messages, limit=limit)
    if not facts:
        return ""

    fact_lines = [
        f'- {fact["confidence"]} confidence | earlier user turn: "{fact["text"]}"'
        for fact in facts
    ]
    return "<prior_user_facts>\n" + "\n".join(fact_lines) + "\n</prior_user_facts>"


def build_follow_up_context_guidance(
    messages: Optional[list[dict]], limit: int = 3
) -> str:
    facts_block = build_relevant_prior_user_facts_block(messages, limit=limit)
    if not facts_block:
        return ""

    return (
        "### Follow-Up Context Guidance:\n"
        "- Reuse relevant user-provided facts from earlier turns before falling back to a generic answer.\n"
        "- If a retrieved policy, rule, or source uses a narrower term than the user's earlier wording, explicitly mention the mismatch and ask one short clarification instead of assuming they are identical.\n"
        "- Ignore these facts when they are not relevant to the latest user request.\n"
        f"{facts_block}"
    )


def build_session_user_memory_prompt(
    messages: Optional[list[dict]],
    stored_facts: Optional[list[dict[str, str]]] = None,
    limit: int = _SESSION_USER_FACTS_PROMPT_LIMIT,
) -> str:
    facts = get_relevant_session_user_facts(messages, stored_facts=stored_facts, limit=limit)
    if not facts:
        return ""

    fact_lines = [
        f'- {fact["confidence"]} confidence | prior user context: "{fact["text"]}"'
        for fact in facts
    ]

    return (
        "### Session User Memory:\n"
        "- Treat the items below as user-provided context established earlier in this chat.\n"
        "- Reuse them when they materially help answer the current turn.\n"
        "- If sources or policies use narrower or slightly different terminology, reconcile the difference explicitly or ask one short clarification.\n"
        "<session_user_memory>\n"
        + "\n".join(fact_lines)
        + "\n</session_user_memory>"
    )


def get_task_model_id(
    default_model_id: str, task_model: str, task_model_external: str, models
) -> str:
    # Set the task model
    task_model_id = default_model_id
    # Check if the user has a custom task model and use that model
    if models[task_model_id].get("connection_type") == "local":
        if task_model and task_model in models:
            task_model_id = task_model
    else:
        if task_model_external and task_model_external in models:
            task_model_id = task_model_external

    return task_model_id


def prompt_variables_template(template: str, variables: dict[str, str]) -> str:
    for variable, value in variables.items():
        template = template.replace(variable, value)
    return template


def prompt_template(template: str, user: Optional[Any] = None) -> str:

    USER_VARIABLES = {}

    if user:
        if hasattr(user, "model_dump"):
            user = user.model_dump()

        if isinstance(user, dict):
            user_info = user.get("info", {}) or {}
            birth_date = user.get("date_of_birth")
            age = None

            if birth_date:
                try:
                    # If birth_date is str, convert to datetime
                    if isinstance(birth_date, str):
                        birth_date = datetime.strptime(birth_date, "%Y-%m-%d")

                    today = datetime.now()
                    age = (
                        today.year
                        - birth_date.year
                        - (
                            (today.month, today.day)
                            < (birth_date.month, birth_date.day)
                        )
                    )
                except Exception as e:
                    pass

            USER_VARIABLES = {
                "name": str(user.get("name")),
                "email": str(user.get("email")),
                "location": str(user_info.get("location")),
                "bio": str(user.get("bio")),
                "gender": str(user.get("gender")),
                "birth_date": str(birth_date),
                "age": str(age),
            }

    # Get the current date
    current_date = datetime.now()

    # Format the date to YYYY-MM-DD
    formatted_date = current_date.strftime("%Y-%m-%d")
    formatted_time = current_date.strftime("%I:%M:%S %p")
    formatted_weekday = current_date.strftime("%A")

    template = template.replace("{{CURRENT_DATE}}", formatted_date)
    template = template.replace("{{CURRENT_TIME}}", formatted_time)
    template = template.replace(
        "{{CURRENT_DATETIME}}", f"{formatted_date} {formatted_time}"
    )
    template = template.replace("{{CURRENT_WEEKDAY}}", formatted_weekday)

    template = template.replace("{{USER_NAME}}", USER_VARIABLES.get("name", "Unknown"))
    template = template.replace(
        "{{USER_EMAIL}}", USER_VARIABLES.get("email", "Unknown")
    )
    template = template.replace("{{USER_BIO}}", USER_VARIABLES.get("bio", "Unknown"))
    template = template.replace(
        "{{USER_GENDER}}", USER_VARIABLES.get("gender", "Unknown")
    )
    template = template.replace(
        "{{USER_BIRTH_DATE}}", USER_VARIABLES.get("birth_date", "Unknown")
    )
    template = template.replace(
        "{{USER_AGE}}", str(USER_VARIABLES.get("age", "Unknown"))
    )
    template = template.replace(
        "{{USER_LOCATION}}", USER_VARIABLES.get("location", "Unknown")
    )

    return template


def replace_prompt_variable(template: str, prompt: str) -> str:
    def replacement_function(match):
        full_match = match.group(
            0
        ).lower()  # Normalize to lowercase for consistent handling
        start_length = match.group(1)
        end_length = match.group(2)
        middle_length = match.group(3)

        if full_match == "{{prompt}}":
            return prompt
        elif start_length is not None:
            return prompt[: int(start_length)]
        elif end_length is not None:
            return prompt[-int(end_length) :]
        elif middle_length is not None:
            middle_length = int(middle_length)
            if len(prompt) <= middle_length:
                return prompt
            start = prompt[: math.ceil(middle_length / 2)]
            end = prompt[-math.floor(middle_length / 2) :]
            return f"{start}...{end}"
        return ""

    # Updated regex pattern to make it case-insensitive with the `(?i)` flag
    pattern = r"(?i){{prompt}}|{{prompt:start:(\d+)}}|{{prompt:end:(\d+)}}|{{prompt:middletruncate:(\d+)}}"
    template = re.sub(pattern, replacement_function, template)
    return template


def truncate_content(content: str, max_chars: int, mode: str = "middletruncate") -> str:
    """Truncate a string to max_chars using the specified mode.

    Modes:
        - middletruncate: keep beginning and end, join with '...'
        - start: keep first max_chars characters
        - end: keep last max_chars characters
    """
    if not content or len(content) <= max_chars:
        return content

    if mode == "start":
        return content[:max_chars]
    elif mode == "end":
        return content[-max_chars:]
    else:  # middletruncate
        half = max_chars // 2
        return f"{content[:half]}...{content[-(max_chars - half):]}"


def apply_content_filter(messages: list[dict], filter_str: str) -> list[dict]:
    """Apply a content filter to each message's content.

    filter_str is like 'middletruncate:500', 'start:200', or 'end:200'.
    Returns a new list with truncated content (original messages are not mutated).
    """
    parts = filter_str.split(":")
    if len(parts) != 2:
        return messages

    mode = parts[0].lower()
    try:
        max_chars = int(parts[1])
    except ValueError:
        return messages

    if mode not in ("middletruncate", "start", "end"):
        return messages

    result = []
    for msg in messages:
        new_msg = dict(msg)
        if isinstance(new_msg.get("content"), str):
            new_msg["content"] = truncate_content(new_msg["content"], max_chars, mode)
        elif isinstance(new_msg.get("content"), list):
            new_content = []
            for item in new_msg["content"]:
                if isinstance(item, dict) and item.get("type") == "text":
                    new_item = dict(item)
                    new_item["text"] = truncate_content(
                        item.get("text", ""), max_chars, mode
                    )
                    new_content.append(new_item)
                else:
                    new_content.append(item)
            new_msg["content"] = new_content
        result.append(new_msg)
    return result


def replace_messages_variable(
    template: str, messages: Optional[list[dict]] = None
) -> str:
    def replacement_function(match):
        # Groups: (1) filter for bare MESSAGES
        #         (2) START count, (3) filter for START
        #         (4) END count,   (5) filter for END
        #         (6) MIDDLE count,(7) filter for MIDDLE
        bare_filter = match.group(1)
        start_length = match.group(2)
        start_filter = match.group(3)
        end_length = match.group(4)
        end_filter = match.group(5)
        middle_length = match.group(6)
        middle_filter = match.group(7)

        # If messages is None, handle it as an empty list
        if messages is None:
            return ""

        # Select messages based on the variant
        if start_length is not None:
            selected = messages[: int(start_length)]
            content_filter = start_filter
        elif end_length is not None:
            selected = messages[-int(end_length) :]
            content_filter = end_filter
        elif middle_length is not None:
            mid = int(middle_length)
            if len(messages) <= mid:
                selected = messages
            else:
                half = mid // 2
                start_msgs = messages[:half]
                end_msgs = messages[-half:] if mid % 2 == 0 else messages[-(half + 1) :]
                selected = start_msgs + end_msgs
            content_filter = middle_filter
        else:
            # Bare {{MESSAGES}} or {{MESSAGES|filter}}
            selected = messages
            content_filter = bare_filter

        # Apply content filter if present
        if content_filter:
            selected = apply_content_filter(selected, content_filter)

        return get_messages_content(selected)

    template = re.sub(
        r"(?:"
        r"\{\{MESSAGES(?:\|(\w+:\d+))?\}\}"
        r"|\{\{MESSAGES:START:(\d+)(?:\|(\w+:\d+))?\}\}"
        r"|\{\{MESSAGES:END:(\d+)(?:\|(\w+:\d+))?\}\}"
        r"|\{\{MESSAGES:MIDDLETRUNCATE:(\d+)(?:\|(\w+:\d+))?\}\}"
        r")",
        replacement_function,
        template,
    )

    return template


# {{prompt:middletruncate:8000}}


def rag_template(template: str, context: str, query: str):
    if template.strip() == "":
        template = DEFAULT_RAG_TEMPLATE

    template = prompt_template(template)

    if "[context]" not in template and "{{CONTEXT}}" not in template:
        log.debug(
            "WARNING: The RAG template does not contain the '[context]' or '{{CONTEXT}}' placeholder."
        )

    if "<context>" in context and "</context>" in context:
        log.debug(
            "WARNING: Potential prompt injection attack: the RAG "
            "context contains '<context>' and '</context>'. This might be "
            "nothing, or the user might be trying to hack something."
        )

    query_placeholders = []
    if "[query]" in context:
        query_placeholder = "{{QUERY" + str(uuid.uuid4()) + "}}"
        template = template.replace("[query]", query_placeholder)
        query_placeholders.append((query_placeholder, "[query]"))

    if "{{QUERY}}" in context:
        query_placeholder = "{{QUERY" + str(uuid.uuid4()) + "}}"
        template = template.replace("{{QUERY}}", query_placeholder)
        query_placeholders.append((query_placeholder, "{{QUERY}}"))

    template = template.replace("[context]", context)
    template = template.replace("{{CONTEXT}}", context)

    template = template.replace("[query]", query)
    template = template.replace("{{QUERY}}", query)

    for query_placeholder, original_placeholder in query_placeholders:
        template = template.replace(query_placeholder, original_placeholder)

    return template


def title_generation_template(
    template: str, messages: list[dict], user: Optional[Any] = None
) -> str:

    prompt = get_last_user_message(messages)
    template = replace_prompt_variable(template, prompt)
    template = replace_messages_variable(template, messages)

    template = prompt_template(template, user)

    return template


def follow_up_generation_template(
    template: str, messages: list[dict], user: Optional[Any] = None
) -> str:
    prompt = get_last_user_message(messages)
    template = replace_prompt_variable(template, prompt)
    template = replace_messages_variable(template, messages)

    template = prompt_template(template, user)
    return template


def tags_generation_template(
    template: str, messages: list[dict], user: Optional[Any] = None
) -> str:
    prompt = get_last_user_message(messages)
    template = replace_prompt_variable(template, prompt)
    template = replace_messages_variable(template, messages)

    template = prompt_template(template, user)
    return template


def image_prompt_generation_template(
    template: str, messages: list[dict], user: Optional[Any] = None
) -> str:
    prompt = get_last_user_message(messages)
    template = replace_prompt_variable(template, prompt)
    template = replace_messages_variable(template, messages)

    template = prompt_template(template, user)
    return template


def emoji_generation_template(
    template: str, prompt: str, user: Optional[Any] = None
) -> str:
    template = replace_prompt_variable(template, prompt)
    template = prompt_template(template, user)

    return template


def autocomplete_generation_template(
    template: str,
    prompt: str,
    messages: Optional[list[dict]] = None,
    type: Optional[str] = None,
    user: Optional[Any] = None,
) -> str:
    template = template.replace("{{TYPE}}", type if type else "")
    template = replace_prompt_variable(template, prompt)
    template = replace_messages_variable(template, messages)

    template = prompt_template(template, user)
    return template


def query_generation_template(
    template: str, messages: list[dict], user: Optional[Any] = None
) -> str:
    prompt = get_last_user_message(messages)
    template = replace_prompt_variable(template, prompt)
    template = replace_messages_variable(template, messages)
    prior_facts_block = build_relevant_prior_user_facts_block(messages)
    if prior_facts_block:
        template = (
            f"{template}\n\n### Relevant Prior User Facts:\n"
            "Use these earlier user facts when they materially narrow the latest search request. "
            "If the authoritative source may use narrower terminology than the user's wording, "
            "preserve the user's facts in the search queries and add the narrower term only when "
            "it helps verify the mapping.\n"
            f"{prior_facts_block}\n"
        )

    template = prompt_template(template, user)
    return template


def moa_response_generation_template(
    template: str, prompt: str, responses: list[str]
) -> str:
    def replacement_function(match):
        full_match = match.group(0)
        start_length = match.group(1)
        end_length = match.group(2)
        middle_length = match.group(3)

        if full_match == "{{prompt}}":
            return prompt
        elif start_length is not None:
            return prompt[: int(start_length)]
        elif end_length is not None:
            return prompt[-int(end_length) :]
        elif middle_length is not None:
            middle_length = int(middle_length)
            if len(prompt) <= middle_length:
                return prompt
            start = prompt[: math.ceil(middle_length / 2)]
            end = prompt[-math.floor(middle_length / 2) :]
            return f"{start}...{end}"
        return ""

    template = re.sub(
        r"{{prompt}}|{{prompt:start:(\d+)}}|{{prompt:end:(\d+)}}|{{prompt:middletruncate:(\d+)}}",
        replacement_function,
        template,
    )

    responses = [f'"""{response}"""' for response in responses]
    responses = "\n\n".join(responses)

    template = template.replace("{{responses}}", responses)
    return template


def tools_function_calling_generation_template(template: str, tools_specs: str) -> str:
    template = template.replace("{{TOOLS}}", tools_specs)
    return template
