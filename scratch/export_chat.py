import json
import os

transcript_path = r"C:\Users\hansa\.gemini\antigravity-ide\brain\95905fae-043d-4cae-a4fb-12e3f5630afd\.system_generated\logs\transcript_full.jsonl"
output_path = r"C:\834_Excel_Backend\chat_history.txt"

with open(transcript_path, 'r', encoding='utf-8') as f_in, open(output_path, 'w', encoding='utf-8') as f_out:
    f_out.write("--- 834 EXCEL BACKEND - CHAT HISTORY ---\n\n")
    for line in f_in:
        try:
            data = json.loads(line)
            step_type = data.get("type", "")
            
            if step_type == "USER_INPUT":
                f_out.write("========================================================\n")
                f_out.write(f"YOU:\n{data.get('content', '')}\n")
                f_out.write("========================================================\n\n")
            elif step_type == "PLANNER_RESPONSE":
                f_out.write(f"ASSISTANT:\n{data.get('content', '')}\n\n")
        except json.JSONDecodeError:
            continue

print("Chat history exported successfully!")
