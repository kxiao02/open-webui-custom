import asyncio
import json
import logging
from typing import Dict, List, Optional
from uuid import uuid4

from redis.asyncio import Redis

from open_webui.env import REDIS_KEY_PREFIX

log = logging.getLogger(__name__)

# A dictionary to keep track of active tasks
tasks: Dict[str, asyncio.Task] = {}
item_tasks = {}


REDIS_TASKS_KEY = f"{REDIS_KEY_PREFIX}:tasks"
REDIS_ITEM_TASKS_KEY = f"{REDIS_KEY_PREFIX}:tasks:item"
REDIS_PUBSUB_CHANNEL = f"{REDIS_KEY_PREFIX}:tasks:commands"
REDIS_TASK_INSTANCE_KEY = f"{REDIS_KEY_PREFIX}:tasks:instance"

TASK_INSTANCE_HEARTBEAT_TTL_SECS = 120
TASK_INSTANCE_HEARTBEAT_INTERVAL_SECS = 30


def _decode_redis_value(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def _serialize_task_record(item_id: Optional[str], instance_id: Optional[str]) -> str:
    return json.dumps(
        {
            "item_id": item_id or "",
            "instance_id": instance_id or "",
        }
    )


def _deserialize_task_record(value) -> dict[str, str]:
    raw_value = _decode_redis_value(value)

    try:
        payload = json.loads(raw_value)
    except Exception:
        payload = None

    if isinstance(payload, dict):
        return {
            "item_id": str(payload.get("item_id") or ""),
            "instance_id": str(payload.get("instance_id") or ""),
        }

    return {
        "item_id": raw_value,
        "instance_id": "",
    }


def _instance_task_heartbeat_key(instance_id: str) -> str:
    return f"{REDIS_TASK_INSTANCE_KEY}:{instance_id}"


async def redis_task_command_listener(app):
    redis: Redis = app.state.redis
    pubsub = redis.pubsub()
    await pubsub.subscribe(REDIS_PUBSUB_CHANNEL)

    async for message in pubsub.listen():
        if message["type"] != "message":
            continue
        try:
            command = json.loads(message["data"])
            if command.get("action") == "stop":
                task_id = command.get("task_id")
                local_task = tasks.get(task_id)
                if local_task:
                    local_task.cancel()
        except Exception as e:
            log.exception(f"Error handling distributed task command: {e}")


### ------------------------------
### REDIS-ENABLED HANDLERS
### ------------------------------


async def redis_save_task(
    redis: Redis, task_id: str, item_id: Optional[str], instance_id: Optional[str] = None
):
    pipe = redis.pipeline()
    pipe.hset(REDIS_TASKS_KEY, task_id, _serialize_task_record(item_id, instance_id))
    if item_id:
        pipe.sadd(f"{REDIS_ITEM_TASKS_KEY}:{item_id}", task_id)
    await pipe.execute()


async def redis_cleanup_task(redis: Redis, task_id: str, item_id: Optional[str]):
    pipe = redis.pipeline()
    pipe.hdel(REDIS_TASKS_KEY, task_id)
    if not item_id:
        await pipe.execute()
        return

    item_tasks_key = f"{REDIS_ITEM_TASKS_KEY}:{item_id}"
    pipe.srem(item_tasks_key, task_id)
    pipe.scard(item_tasks_key)
    results = await pipe.execute()
    remaining_tasks = int(results[-1] or 0)
    if remaining_tasks == 0:
        await redis.delete(item_tasks_key)


async def redis_list_tasks(redis: Redis) -> List[str]:
    return list(await redis.hkeys(REDIS_TASKS_KEY))


async def redis_list_item_tasks(redis: Redis, item_id: str) -> List[str]:
    return list(await redis.smembers(f"{REDIS_ITEM_TASKS_KEY}:{item_id}"))


async def redis_mark_instance_alive(redis: Redis, instance_id: str):
    await redis.set(
        _instance_task_heartbeat_key(instance_id),
        "1",
        ex=TASK_INSTANCE_HEARTBEAT_TTL_SECS,
    )


async def redis_remove_instance(redis: Redis, instance_id: str):
    await redis.delete(_instance_task_heartbeat_key(instance_id))


async def redis_list_active_instances(redis: Redis) -> List[str]:
    instance_ids: List[str] = []
    async for key in redis.scan_iter(match=f"{REDIS_TASK_INSTANCE_KEY}:*"):
        decoded_key = _decode_redis_value(key)
        _, _, instance_id = decoded_key.rpartition(":")
        if instance_id:
            instance_ids.append(instance_id)
    return instance_ids


async def redis_is_instance_alive(
    redis: Redis, instance_id: str, active_instance_ids: Optional[List[str]] = None
) -> bool:
    if not instance_id:
        return False

    if active_instance_ids is not None:
        return instance_id in active_instance_ids

    return bool(await redis.exists(_instance_task_heartbeat_key(instance_id)))


async def prune_stale_task_metadata(
    redis: Redis, task_ids: List[str], current_instance_id: Optional[str] = None
) -> List[str]:
    if not task_ids:
        return []

    active_instance_ids = await redis_list_active_instances(redis)
    survivors: List[str] = []

    for task_id in task_ids:
        raw_record = await redis.hget(REDIS_TASKS_KEY, task_id)
        if raw_record is None:
            continue

        record = _deserialize_task_record(raw_record)
        item_id = record["item_id"] or None
        owner_instance_id = record["instance_id"]

        if owner_instance_id:
            owner_alive = await redis_is_instance_alive(
                redis, owner_instance_id, active_instance_ids
            )
            if not owner_alive:
                await redis_cleanup_task(redis, task_id, item_id)
                continue

            if current_instance_id and owner_instance_id == current_instance_id:
                local_task = tasks.get(task_id)
                if local_task is None or local_task.done():
                    await redis_cleanup_task(redis, task_id, item_id)
                    continue
        elif current_instance_id:
            local_task = tasks.get(task_id)
            only_current_instance_active = not active_instance_ids or (
                len(active_instance_ids) == 1
                and current_instance_id in active_instance_ids
            )
            if only_current_instance_active and (
                local_task is None or local_task.done()
            ):
                await redis_cleanup_task(redis, task_id, item_id)
                continue

        survivors.append(task_id)

    return survivors


async def task_instance_heartbeat(redis: Redis, instance_id: str):
    try:
        while True:
            await redis_mark_instance_alive(redis, instance_id)
            await asyncio.sleep(TASK_INSTANCE_HEARTBEAT_INTERVAL_SECS)
    except asyncio.CancelledError:
        try:
            await redis_remove_instance(redis, instance_id)
        finally:
            raise


async def redis_send_command(redis: Redis, command: dict):
    command_json = json.dumps(command)
    # RedisCluster doesn't expose publish() directly, but the
    # PUBLISH command broadcasts across all cluster nodes server-side.
    if hasattr(redis, "nodes_manager"):
        await redis.execute_command("PUBLISH", REDIS_PUBSUB_CHANNEL, command_json)
    else:
        await redis.publish(REDIS_PUBSUB_CHANNEL, command_json)


async def cleanup_task(redis, task_id: str, id=None):
    """
    Remove a completed or canceled task from the global `tasks` dictionary.
    """
    if redis:
        try:
            await redis_cleanup_task(redis, task_id, id)
        except Exception as exc:
            log.warning("Failed to clean Redis metadata for task %s: %s", task_id, exc)

    tasks.pop(task_id, None)  # Remove the task if it exists

    # If an ID is provided, remove the task from the item_tasks dictionary
    if id is not None and task_id in item_tasks.get(id, []):
        item_tasks[id].remove(task_id)
        if not item_tasks[id]:  # If no tasks left for this ID, remove the entry
            item_tasks.pop(id, None)


async def create_task(redis, coroutine, id=None, instance_id: Optional[str] = None):
    """
    Create a new asyncio task and add it to the global task dictionary.
    """
    task_id = str(uuid4())  # Generate a unique ID for the task
    task = asyncio.create_task(coroutine)  # Create the task

    # Add a done callback for cleanup
    task.add_done_callback(
        lambda t: asyncio.create_task(cleanup_task(redis, task_id, id))
    )
    tasks[task_id] = task

    # If an ID is provided, associate the task with that ID
    if id is not None:
        if item_tasks.get(id):
            item_tasks[id].append(task_id)
        else:
            item_tasks[id] = [task_id]

    if redis:
        try:
            await redis_save_task(redis, task_id, id, instance_id)
        except Exception as exc:
            log.warning("Failed to persist Redis metadata for task %s: %s", task_id, exc)

    return task_id, task


async def list_tasks(redis, current_instance_id: Optional[str] = None):
    """
    List all currently active task IDs.
    """
    if redis:
        task_ids = [_decode_redis_value(task_id) for task_id in await redis_list_tasks(redis)]
        return await prune_stale_task_metadata(redis, task_ids, current_instance_id)
    return list(tasks.keys())


async def list_task_ids_by_item_id(redis, id, current_instance_id: Optional[str] = None):
    """
    List all tasks associated with a specific ID.
    """
    if redis:
        task_ids = [
            _decode_redis_value(task_id) for task_id in await redis_list_item_tasks(redis, id)
        ]
        return await prune_stale_task_metadata(redis, task_ids, current_instance_id)
    return item_tasks.get(id, [])


async def stop_task(redis, task_id: str):
    """
    Cancel a running task and remove it from the global task list.
    """
    if redis:
        # PUBSUB: All instances check if they have this task, and stop if so.
        await redis_send_command(
            redis,
            {
                "action": "stop",
                "task_id": task_id,
            },
        )
        # Optionally check if task_id still in Redis a few moments later for feedback?
        return {"status": True, "message": f"Stop signal sent for {task_id}"}

    task = tasks.pop(task_id, None)
    if not task:
        return {"status": False, "message": f"Task with ID {task_id} not found."}

    task.cancel()  # Request task cancellation
    try:
        await task  # Wait for the task to handle the cancellation
    except asyncio.CancelledError:
        # Task successfully canceled
        return {"status": True, "message": f"Task {task_id} successfully stopped."}

    if task.cancelled() or task.done():
        return {"status": True, "message": f"Task {task_id} successfully cancelled."}

    return {"status": True, "message": f"Cancellation requested for {task_id}."}


async def stop_item_tasks(redis: Redis, item_id: str):
    """
    Stop all tasks associated with a specific item ID.
    """
    task_ids = await list_task_ids_by_item_id(redis, item_id)
    if not task_ids:
        return {"status": True, "message": f"No tasks found for item {item_id}."}

    for task_id in task_ids:
        result = await stop_task(redis, task_id)
        if not result["status"]:
            return result  # Return the first failure

    return {"status": True, "message": f"All tasks for item {item_id} stopped."}


async def has_active_tasks(redis, chat_id: str) -> bool:
    """Check if a chat has any active tasks."""
    task_ids = await list_task_ids_by_item_id(redis, chat_id)
    return len(task_ids) > 0


async def get_active_chat_ids(redis, chat_ids: List[str]) -> List[str]:
    """Filter a list of chat_ids to only those with active tasks."""
    active = []
    for chat_id in chat_ids:
        if await has_active_tasks(redis, chat_id):
            active.append(chat_id)
    return active
