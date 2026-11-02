
from core.antigravity import AntigravityClient


def show_progress(event):
    if event.get("event") == "step_update":
        step = event.get("step_update", {})

        if step.get("text_delta"):
            print(
                step["text_delta"],
                end="",
                flush=True
            )


jarvis = AntigravityClient(
    model="gemini-4-argon-medium",
    working_dir=".",
    mode="plan"
)

try:
    conversation_id = jarvis.start()

    print("\nJARVIS ONLINE")
    print("Conversation:", conversation_id)

    answer1 = jarvis.ask(
        "Benim adım Bedirhan. Senin adın JARVIS.",
        on_event=show_progress
    )

    print("\n\nJARVIS:", answer1)

    answer2 = jarvis.ask(
        "Benim adım ne ve senin adın ne?",
        on_event=show_progress
    )

    print("\n\nJARVIS:", answer2)

finally:
    jarvis.close()
    print("\nJARVIS OFFLINE")
