import pathlib

P = pathlib.Path('c:/Users/kavin/Desktop/PCMS/backend/app/services/call_flow.py')
lines = P.read_text(encoding='utf-8-sig').splitlines(keepends=True)

# Fix line 319 (index 318): capture_final_answer(( -> capture_turn(
print(f'Before line 319: {lines[318]!r}')
lines[318] = lines[318].replace('capture_final_answer((', 'capture_turn(')
print(f'After line 319: {lines[318]!r}')

# Find the end of capture_turn (line with "return bytes(pcm), detector.has_speech")
end_idx = None
for i in range(318, len(lines)):
    if 'return bytes(pcm), detector.has_speech' in lines[i]:
        end_idx = i
        break

if end_idx is None:
    print('ERROR: could not find end of capture_turn')
    exit(1)

print(f'End of capture_turn at line {end_idx+1}: {lines[end_idx]!r}')

# Find the next blank line after end_idx
insert_after = end_idx + 1
while insert_after < len(lines) and lines[insert_after].strip() != '':
    insert_after += 1

print(f'Inserting after line {insert_after+1}')

new_func = '''

async def capture_final_answer(
    inbox: asyncio.Queue, session, settings: Settings
) -> tuple[bytes, bool]:
    """Collect the final open-ended answer in a fixed window.

    Unlike capture_turn, this uses FINAL_ANSWER_SEC (not the silence
    detector) so a patient who pauses mid-sentence is not cut off. The
    call is closed after the window either way.
    """
    deadline = time.monotonic() + settings.final_answer_sec
    pcm = bytearray()
    last_frame = time.monotonic()
    had_speech = False

    while time.monotonic() < deadline:
        remaining_gap = settings.turn_gap_sec - (time.monotonic() - last_frame)
        try:
            kind, item = await asyncio.wait_for(
                inbox.get(), timeout=max(0.05, remaining_gap)
            )
        except asyncio.TimeoutError:
            break

        last_frame = time.monotonic()
        if kind == "closed":
            raise CallEnded("stream closed during final answer")
        if kind == "event":  # 'stop' -> the provider ended the call
            break
        if session.speaking:
            continue  # our own voice; not patient audio

        chunk = recordings_service.decode_chunk(session.encoding, item)
        if chunk:
            pcm.extend(chunk)
            had_speech = True

    return bytes(pcm), had_speech
'''

# Insert the new function
lines.insert(insert_after + 1, new_func)

P.write_text(''.join(lines), encoding='utf-8-sig')
print(f'Done. New total lines: {len(lines)}')
