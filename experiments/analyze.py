import argparse
import asyncio
import os
from pathlib import Path

import asyncpg
import pandas as pd
from matplotlib.figure import Figure

RESULTS = Path("experiments/results")
DATABASE_URL = os.getenv(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5433/flight_delay"
)
WARMUP_SEC = 15


def rate(df: pd.DataFrame, column: str, t0: float, window: int = 5) -> pd.DataFrame:
    out = pd.DataFrame({"t": df["ts"] - t0})
    out["rate"] = df[column].diff() / df["ts"].diff()
    out["rate"] = out["rate"].rolling(window, min_periods=1).mean()
    return out


async def load_tasks(label: str) -> pd.DataFrame:
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        rows = await conn.fetch(
            """
            SELECT extract(epoch FROM created_at)  AS created,
                   extract(epoch FROM started_at)  AS started,
                   extract(epoch FROM finished_at) AS finished,
                   worker
            FROM tasks WHERE source = $1 AND status = 'DONE'
            """,
            label,
        )
    finally:
        await conn.close()
    df = pd.DataFrame(
        [dict(r) for r in rows], columns=["created", "started", "finished", "worker"]
    )
    return df.astype({"created": float, "started": float, "finished": float})


def load_run(label: str) -> dict:
    directory = RESULTS / label
    rabbit = pd.read_csv(directory / "rabbit.csv")
    source = pd.concat(pd.read_csv(p) for p in directory.glob("source_*.csv"))
    workers = {p.stem: pd.read_csv(p) for p in directory.glob("worker_*.csv")}
    tasks = asyncio.run(load_tasks(label))
    t0 = source["ts"].min()
    t_end = source["ts"].max()
    return {
        "label": label,
        "rabbit": rabbit,
        "source": source,
        "workers": workers,
        "tasks": tasks,
        "t0": t0,
        "t_end": t_end,
    }


def summarize(run: dict, start: float, end: float) -> dict:
    t0 = run["t0"]
    src = run["source"]
    src_win = src[(src["ts"] - t0 >= start) & (src["ts"] - t0 <= end)]
    source_rate = (src_win["published"].iloc[-1] - src_win["published"].iloc[0]) / (
        src_win["ts"].iloc[-1] - src_win["ts"].iloc[0]
    )

    tasks = run["tasks"]
    done = tasks[(tasks["finished"] - t0 >= start) & (tasks["finished"] - t0 <= end)]
    throughput = len(done) / (end - start)
    e2e = done["finished"] - done["created"]
    inference = done["finished"] - done["started"]

    rabbit = run["rabbit"]
    rb = rabbit[(rabbit["ts"] - t0 >= start) & (rabbit["ts"] - t0 <= end)]
    worker_mem = max(w["rss_mb"].max() for w in run["workers"].values())

    return {
        "сценарий": run["label"],
        "окно, с": f"{start:.0f}-{end:.0f}",
        "скорость источника, задач/с": round(source_rate, 1),
        "пропускная способность воркеров, задач/с": round(throughput, 1),
        "глубина очереди макс": int(rb["messages"].max()) if len(rb) else 0,
        "глубина очереди средн": round(rb["messages"].mean(), 1) if len(rb) else 0,
        "время задачи (e2e) средн, с": round(e2e.mean(), 2),
        "время задачи (e2e) p95, с": round(e2e.quantile(0.95), 2),
        "время в воркере средн, с": round(inference.mean(), 3),
        "отказов брокера (nack)": int(
            src_win["rejected"].iloc[-1] - src_win["rejected"].iloc[0]
        ),
        "память воркера макс, МБ": round(worker_mem, 1),
        "память источника макс, МБ": round(src["rss_mb"].max(), 1),
        "память RabbitMQ макс, МБ": round(rabbit["rabbit_mem_mb"].max(), 1),
    }


def plot_run(run: dict, scale_at: float | None) -> Path:
    t0 = run["t0"]
    fig = Figure(figsize=(13, 10))
    axes = fig.subplots(2, 2)
    fig.suptitle(f"Сценарий {run['label']}", fontsize=14)

    ax = axes[0][0]
    rabbit = run["rabbit"]
    ax.plot(rabbit["ts"] - t0, rabbit["messages"], label="всего в очереди")
    ax.plot(
        rabbit["ts"] - t0, rabbit["unacked"], label="у воркеров (unacked)", alpha=0.7
    )
    ax.set_title("Глубина очереди")
    ax.set_ylabel("сообщений")

    ax = axes[0][1]
    src_rate = rate(run["source"], "published", t0)
    ax.plot(src_rate["t"], src_rate["rate"], label="источник (принято брокером)")
    for name, w in run["workers"].items():
        w_rate = rate(w, "processed", t0)
        ax.plot(w_rate["t"], w_rate["rate"], label=f"{name} (обработано)", alpha=0.7)
    ax.set_title("Скорость, задач/с (окно 5 с)")
    if src_rate["rate"].max() > 200:
        ax.set_yscale("log")

    ax = axes[1][0]
    ax.plot(run["source"]["ts"] - t0, run["source"]["rss_mb"], label="источник")
    for name, w in run["workers"].items():
        ax.plot(w["ts"] - t0, w["rss_mb"], label=name)
    ax.plot(rabbit["ts"] - t0, rabbit["rabbit_mem_mb"], label="RabbitMQ")
    ax.set_title("Память, МБ")

    ax = axes[1][1]
    tasks = run["tasks"].sort_values("finished")
    ax.scatter(
        tasks["finished"] - t0,
        tasks["finished"] - tasks["created"],
        s=2,
        label="e2e (ожидание + inference)",
    )
    ax.scatter(
        tasks["finished"] - t0,
        tasks["finished"] - tasks["started"],
        s=2,
        label="в воркере (получение -> запись в БД)",
    )
    ax.set_title("Время выполнения задачи, с")
    ax.set_yscale("log")

    for row in axes:
        for ax in row:
            ax.set_xlabel("секунды от старта источника")
            ax.axvline(run["t_end"] - t0, color="grey", linestyle="--", linewidth=0.8)
            if scale_at:
                ax.axvline(scale_at, color="red", linestyle=":", linewidth=1)
            ax.legend(fontsize=8)
            ax.grid(alpha=0.3)

    fig.tight_layout()
    path = RESULTS / f"{run['label']}.png"
    fig.savefig(path, dpi=110)
    return path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("labels", nargs="+")
    parser.add_argument(
        "--scale-at", type=float, default=60, help="для *_scale прогонов"
    )
    args = parser.parse_args()

    rows = []
    for label in args.labels:
        run = load_run(label)
        end = run["t_end"] - run["t0"]
        scale_at = args.scale_at if "scale" in label else None
        if scale_at:
            rows.append(summarize(run, WARMUP_SEC, scale_at))
            rows.append(summarize(run, scale_at + WARMUP_SEC, end))
        else:
            rows.append(summarize(run, WARMUP_SEC, end))
        print("plot:", plot_run(run, scale_at))

    table = pd.DataFrame(rows).set_index("сценарий").T
    header = "| показатель | " + " | ".join(table.columns) + " |"
    divider = "|---" * (len(table.columns) + 1) + "|"
    body = [
        f"| {name} | " + " | ".join(str(v) for v in values) + " |"
        for name, values in table.iterrows()
    ]
    markdown = "\n".join([header, divider, *body])
    (RESULTS / "summary.md").write_text(markdown + "\n")
    print(markdown)


if __name__ == "__main__":
    main()
