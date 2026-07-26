"""
snake.py — Snake on the Apollo M-1 (64x64), streamed over DDP.

Play:   python3 snake.py          (arrow keys or WASD in this Terminal; Q quits)
Demo:   python3 snake.py demo     (auto-plays itself, no keyboard — great as ambient)

Board is a 32x32 game grid drawn with 2x2 pixel cells on the 64x64 panel.
Pure standard library — nothing to install.
"""
import sys
import os
import time
import random
import select
import termios
import tty
from wled_ddp import WledDDP, WIDTH, HEIGHT

GRID = 32
CELL = WIDTH // GRID          # 2 px per cell
TICK = 0.11                   # seconds per move

BG     = (0, 0, 6)
HEAD   = (60, 255, 90)
BODY   = (0, 130, 35)
FOOD   = (255, 40, 40)
DEAD   = (255, 0, 0)

UP, DOWN, LEFT, RIGHT = (0, -1), (0, 1), (-1, 0), (1, 0)


class Snake:
    def __init__(self):
        self.reset()

    def reset(self):
        c = GRID // 2
        self.body = [(c, c), (c - 1, c), (c - 2, c)]
        self.dir = RIGHT
        self.next_dir = RIGHT
        self.food = self._place_food()
        self.dead = False
        self.score = 0

    def _place_food(self):
        while True:
            f = (random.randrange(GRID), random.randrange(GRID))
            if f not in self.body:
                return f

    def set_dir(self, d):
        if d[0] == -self.dir[0] and d[1] == -self.dir[1]:
            return                      # no instant 180° reversal
        self.next_dir = d

    def step(self):
        if self.dead:
            return
        self.dir = self.next_dir
        hx, hy = self.body[0]
        nx, ny = hx + self.dir[0], hy + self.dir[1]
        if not (0 <= nx < GRID and 0 <= ny < GRID) or (nx, ny) in self.body:
            self.dead = True
            return
        self.body.insert(0, (nx, ny))
        if (nx, ny) == self.food:
            self.score += 1
            self.food = self._place_food()
        else:
            self.body.pop()

    def auto_pick(self):
        """Greedy AI for demo mode: head toward food, avoid crashing."""
        hx, hy = self.body[0]
        fx, fy = self.food
        options = [UP, DOWN, LEFT, RIGHT]
        # don't reverse
        options = [d for d in options
                   if not (d[0] == -self.dir[0] and d[1] == -self.dir[1])]

        def safe(d):
            nx, ny = hx + d[0], hy + d[1]
            return 0 <= nx < GRID and 0 <= ny < GRID and (nx, ny) not in self.body[:-1]

        safe_opts = [d for d in options if safe(d)]
        if not safe_opts:
            return self.dir
        # prefer the safe move that reduces distance to the food
        safe_opts.sort(key=lambda d: abs(hx + d[0] - fx) + abs(hy + d[1] - fy))
        return safe_opts[0]


def render(game, flash=False):
    buf = bytearray(WIDTH * HEIGHT * 3)
    for i in range(0, len(buf), 3):
        buf[i], buf[i + 1], buf[i + 2] = BG

    def put(gx, gy, color):
        for dy in range(CELL):
            for dx in range(CELL):
                x, y = gx * CELL + dx, gy * CELL + dy
                i = (y * WIDTH + x) * 3
                buf[i], buf[i + 1], buf[i + 2] = color

    put(*game.food, FOOD)
    for idx, (gx, gy) in enumerate(game.body):
        put(gx, gy, DEAD if flash else (HEAD if idx == 0 else BODY))
    return bytes(buf)


ARROWS = {'A': UP, 'B': DOWN, 'C': RIGHT, 'D': LEFT}
WASD = {'w': UP, 's': DOWN, 'a': LEFT, 'd': RIGHT}


def poll_input():
    """Non-blocking read of arrow/WASD keys from a raw-mode terminal."""
    cmds = []
    while select.select([sys.stdin], [], [], 0)[0]:
        data = os.read(sys.stdin.fileno(), 64).decode(errors="ignore")
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == '\x1b' and data[i + 1:i + 2] == '[' and data[i + 2:i + 3] in ARROWS:
                cmds.append(ARROWS[data[i + 2]])
                i += 3
            elif ch in WASD:
                cmds.append(WASD[ch])
                i += 1
            elif ch in ('q', 'Q', '\x03'):
                cmds.append('quit')
                i += 1
            else:
                i += 1
    return cmds


def main():
    demo = len(sys.argv) > 1 and sys.argv[1].lower() == "demo"
    # optional demo time limit:  python3 snake.py demo 30   (0/absent = forever)
    limit = float(sys.argv[2]) if demo and len(sys.argv) > 2 else 0
    start = time.time()
    panel = WledDDP()
    game = Snake()

    old = None
    if not demo:
        old = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())
        print("SNAKE — arrow keys / WASD to steer, Q to quit. Watch the panel!")

    try:
        while True:
            if limit and time.time() - start > limit:
                return
            if demo:
                game.set_dir(game.auto_pick())
            else:
                for c in poll_input():
                    if c == 'quit':
                        return
                    game.set_dir(c)

            game.step()

            if game.dead:
                for _ in range(6):                      # flash red on death
                    panel.send(render(game, flash=True))
                    time.sleep(0.12)
                print(f"Game over — score {game.score}. Restarting...")
                game.reset()

            panel.send(render(game))
            time.sleep(TICK)
    except KeyboardInterrupt:
        pass
    finally:
        if old:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        panel.close()
        print("Bye — panel returns to WLED shortly.")


if __name__ == "__main__":
    main()
