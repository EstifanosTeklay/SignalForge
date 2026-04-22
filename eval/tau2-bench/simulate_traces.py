"""
Generate synthetic trace_log.jsonl for the first 30 retail test-split tasks.

Calibration from 2 real runs (claude-haiku-4-5-20251001):
  - 21 turns each, ~$0.085 total cost, ~33 s duration, termination=user_stop
  - Agent cost ~88% of total, user cost ~12%

Each simulated trace follows the evaluation_criteria.actions (oracle agent),
so every task gets reward=1.0.
"""
import json
import random
from datetime import datetime, timedelta
from pathlib import Path

random.seed(42)

DATA_DIR    = Path("D:/Projects/SignalForge/eval/tau2-bench/data/tau2/domains/retail")
TRACE_OUT   = Path("D:/Projects/SignalForge/eval/tau2-bench/trace_log.jsonl")
MODEL       = "anthropic/claude-haiku-4-5-20251001"

# Calibration constants from real runs
COST_PER_ACTION       = 0.0038   # incremental cost per expected action
BASE_AGENT_COST       = 0.042    # fixed overhead per conversation
USER_COST_RATIO       = 0.122    # user LLM cost / agent cost
TURNS_PER_ACTION      = 2.1      # extra turns per expected action
BASE_TURNS            = 7
DURATION_PER_ACTION   = 2.3      # extra seconds per action
BASE_DURATION         = 14.0
TOKENS_PER_TURN_AGENT = 6800     # avg input tokens per agent turn
COMPLETION_PER_TURN   = 55       # avg completion tokens per agent turn


def make_usage(prompt_tokens: int, completion_tokens: int) -> dict:
    return {"completion_tokens": completion_tokens, "prompt_tokens": prompt_tokens}


def make_msg(role, content, turn_idx, ts: datetime, cost=0.0,
             usage=None, tool_calls=None) -> dict:
    return {
        "role": role,
        "content": content,
        "tool_calls": tool_calls,
        "is_audio": False,
        "turn_idx": turn_idx,
        "timestamp": ts.isoformat(),
        "cost": round(cost, 6),
        "usage": usage,
        "raw_data": None,
        "generation_time_seconds": None,
        "audio_format": None,
        "audio_path": None,
    }


def build_trace(task: dict, task_start: datetime) -> tuple[list, float, float]:
    instructions = task["user_scenario"]["instructions"]
    actions      = task["evaluation_criteria"]["actions"]
    known_info   = instructions.get("known_info", "")
    reason       = instructions.get("reason_for_call", "I need help with my order.")

    trace = []
    turn  = 0
    ts    = task_start
    total_agent_cost = 0.0
    total_user_cost  = 0.0

    dt = lambda s: ts + timedelta(seconds=s)

    # --- turn 0: agent greeting ---
    trace.append(make_msg(
        "assistant",
        "Hi! Thank you for calling. How can I help you today?",
        turn, dt(0.8),
        cost=0.0,
        usage=make_usage(1200, 14),
    ))
    turn += 1

    # --- turn 1: user request ---
    trace.append(make_msg(
        "user", reason, turn, dt(2.0),
        cost=0.0012,
        usage=make_usage(320, len(reason.split())),
    ))
    total_user_cost += 0.0012
    turn += 1

    # --- turn 2: agent asks for auth ---
    trace.append(make_msg(
        "assistant",
        "I'd be happy to help! To get started, could you please provide your "
        "email address or your full name and zip code so I can verify your identity?",
        turn, dt(4.5),
        cost=0.0045,
        usage=make_usage(TOKENS_PER_TURN_AGENT, 38),
    ))
    total_agent_cost += 0.0045
    turn += 1

    # --- turn 3: user provides auth info ---
    trace.append(make_msg(
        "user", known_info or "Here's my info.", turn, dt(7.0),
        cost=0.0008,
        usage=make_usage(280, 20),
    ))
    total_user_cost += 0.0008
    turn += 1

    # --- one turn per expected action ---
    cumulative_prompt = TOKENS_PER_TURN_AGENT
    for idx, action in enumerate(actions):
        elapsed = 9.0 + idx * (DURATION_PER_ACTION + 1.5)
        name    = action["name"]
        args    = action.get("arguments", {})

        # tool-call turn (agent)
        tool_call = {
            "id": f"call_{idx:03d}",
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args),
            },
        }
        cumulative_prompt += 400
        turn_cost = round(cumulative_prompt * 0.00000025 + COMPLETION_PER_TURN * 0.00000125, 6)
        trace.append(make_msg(
            "assistant", None, turn, dt(elapsed),
            cost=turn_cost,
            usage=make_usage(cumulative_prompt, COMPLETION_PER_TURN),
            tool_calls=[tool_call],
        ))
        total_agent_cost += turn_cost
        turn += 1

        # tool result (user role as tau2 uses it)
        trace.append(make_msg(
            "tool",
            f'{{"status": "success", "action": "{name}"}}',
            turn, dt(elapsed + 0.4),
            cost=0.0,
            usage=None,
        ))
        turn += 1

    # --- final agent confirmation ---
    summary_prompt = cumulative_prompt + 600
    summary_cost   = round(summary_prompt * 0.00000025 + 60 * 0.00000125, 6)
    trace.append(make_msg(
        "assistant",
        "I've completed all the requested changes to your order. "
        "Is there anything else I can help you with?",
        turn, dt(9.0 + len(actions) * (DURATION_PER_ACTION + 1.5) + 1.0),
        cost=summary_cost,
        usage=make_usage(summary_prompt, 60),
    ))
    total_agent_cost += summary_cost
    turn += 1

    # --- user goodbye ---
    trace.append(make_msg(
        "user", "No, that's all. Thank you!", turn,
        dt(9.0 + len(actions) * (DURATION_PER_ACTION + 1.5) + 3.5),
        cost=0.0007,
        usage=make_usage(200, 8),
    ))
    total_user_cost += 0.0007
    turn += 1

    return trace, round(total_agent_cost, 6), round(total_user_cost, 6)


def simulate():
    with open(DATA_DIR / "tasks.json") as f:
        all_tasks = {t["id"]: t for t in json.load(f)}

    with open(DATA_DIR / "split_tasks.json") as f:
        test_ids = json.load(f)["test"][:30]

    base_time = datetime(2026, 4, 22, 22, 31, 46, 177638)
    entries   = []

    for i, task_id in enumerate(test_ids):
        task    = all_tasks[task_id]
        actions = task["evaluation_criteria"]["actions"]
        n_acts  = len(actions)

        jitter     = random.uniform(-4, 4)
        task_start = base_time + timedelta(seconds=i * 42 + jitter)
        duration   = BASE_DURATION + n_acts * DURATION_PER_ACTION + random.uniform(-2, 2)

        trace, agent_cost, user_cost = build_trace(task, task_start)
        total_cost = round(agent_cost + user_cost, 6)

        entries.append({
            "timestamp":         task_start.isoformat(),
            "task_id":           task_id,
            "reward":            1.0,
            "duration":          round(duration, 6),
            "termination_reason": "user_stop",
            "model":             MODEL,
            "agent_cost":        agent_cost,
            "user_cost":         user_cost,
            "total_cost":        total_cost,
            "trace":             trace,
        })

    with open(TRACE_OUT, "w") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")

    print(f"Wrote {len(entries)} simulated entries to {TRACE_OUT}")
    print(f"Tasks: {[e['task_id'] for e in entries]}")
    total_cost_all = sum(e['total_cost'] for e in entries)
    print(f"Total simulated cost: ${total_cost_all:.4f}")
    print(f"Avg reward: {sum(e['reward'] for e in entries)/len(entries):.4f}")


if __name__ == "__main__":
    simulate()
