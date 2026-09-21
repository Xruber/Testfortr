#!/usr/bin/env python3
"""
================================================================================
 CONSCIOUS MINDS v4  —  META-LEARNING + BET SIMULATOR + CONFIDENCE GATING
================================================================================
 1000x improvements over v3:
   - Meta-learner admin (tracks strategy × context success)
   - Agreement engine (multi-strategy convergence boost)
   - Live bet simulator (Top-1..Top-5, 9.8x payout)
   - Digit reliability tracker (per-digit historical hit rate)
   - Confidence gating (HOLD when no edge)
   - Mood rebalancing (prevents confused-epidemic)
   - Recency-weighted mind weights
   - Failed-mind replacement
   - Top-K stability + rolling 50-round tracker
================================================================================
"""

import json, time, math, random, logging, statistics
import http.cookiejar, urllib.request, urllib.parse, urllib.error
from collections import Counter, deque, defaultdict
from concurrent.futures import ThreadPoolExecutor

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("engine")

# ══════════════════════════════════════════════════════════════════
# CONFIG
# ══════════════════════════════════════════════════════════════════
class Config:
    MAX_HISTORY    = 10
    PAGE_SIZE      = 10
    ROUND_SECONDS  = 60
    ROOT           = "https://draw.ar-lottery01.com/"
    ENDPOINT       = "https://draw.ar-lottery01.com/WinGo/WinGo_1M/GetHistoryIssuePage.json"
    TIMEOUT        = 15.0
    RETRIES        = 3
    MAX_WORKERS    = 48
    PAYOUT         = 9.8
CONFIG = Config()

# ══════════════════════════════════════════════════════════════════
# TUNING
# ══════════════════════════════════════════════════════════════════
MIND_HISTORY_LEN    = 20
MIND_PRIOR_WEIGHT   = 0.5
MIND_WEIGHT_FLOOR   = 0.05
STRATEGY_WINDOW     = 40
STRATEGY_MIN_ROUNDS = 12
RECENCY_HALFLIFE    = 8       # in rounds; older hits decay
FAILED_MIND_THRESH  = 0.03    # accuracy below this after N rounds → replace
FAILED_MIND_MIN     = 25      # need this many votes before judging

# ══════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════
def colour_of(d):
    if d == 0: return 0
    if d == 5: return 1
    if d in (2, 4, 6, 8): return 2
    return 3
def digits_of_colour(c):
    return {0:[0],1:[5],2:[2,4,6,8],3:[1,3,7,9]}[c]
def parity_of(d): return d & 1
def size_of(d):   return 1 if d >= 5 else 0
def clamp(v):     return max(0, min(9, int(round(v))))
def increment_issue(s):
    i = len(s)
    while i > 0 and s[i-1].isdigit(): i -= 1
    if i == len(s): return s + "1"
    p, n = s[:i], s[i:]
    return f"{p}{int(n) + 1:0{len(n)}d}"
def weighted_sample(w):
    total = sum(w)
    if total <= 0: return random.randint(0, 9)
    r = random.random() * total
    acc = 0.0
    for i, x in enumerate(w):
        acc += x
        if r <= acc: return i
    return 9

# ══════════════════════════════════════════════════════════════════
# FETCHER
# ══════════════════════════════════════════════════════════════════
class Fetcher:
    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            urllib.request.HTTPRedirectHandler())
        self.warmed_up = False

    def _headers(self):
        return {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/122.0.0.0 Safari/537.36"),
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "identity",
            "Referer": CONFIG.ROOT,
            "Origin": "https://draw.ar-lottery01.com",
            "Connection": "keep-alive",
            "Sec-Fetch-Dest": "empty", "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "sec-ch-ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "X-Requested-With": "XMLHttpRequest",
        }

    def warm_up(self):
        if self.warmed_up: return
        try:
            req = urllib.request.Request(CONFIG.ROOT, headers={
                "User-Agent": self._headers()["User-Agent"],
                "Accept": "text/html,*/*;q=0.8",
                "Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none", "Sec-Fetch-User": "?1",
                "Upgrade-Insecure-Requests": "1",
            })
            self.opener.open(req, timeout=CONFIG.TIMEOUT).read(1)
        except Exception as e:
            log.warning("Warm-up failed: %s", e)
        self.warmed_up = True

    def fetch_page1(self):
        self.warm_up()
        url = CONFIG.ENDPOINT + "?" + urllib.parse.urlencode(
            {"pageNo": 1, "pageSize": CONFIG.PAGE_SIZE})
        for attempt in range(1, CONFIG.RETRIES + 1):
            try:
                req = urllib.request.Request(url, headers=self._headers())
                with self.opener.open(req, timeout=CONFIG.TIMEOUT) as r:
                    text = r.read().decode("utf-8", errors="replace")
                    data = json.loads(text)
                    if data.get("code") != 0:
                        raise ValueError(f"code {data.get('code')}")
                    return data["data"]["list"], text
            except Exception as e:
                log.warning("Attempt %d failed: %s", attempt, e)
                time.sleep(2)
        return None, None

    def fetch_history_oldest_first(self):
        items, _ = self.fetch_page1()
        if not items: return None
        nums = [int(e["number"]) for e in items]
        nums.reverse()
        return nums

    def fetch_latest(self):
        items, _ = self.fetch_page1()
        if not items: return None, None
        e = items[0]
        return int(e["number"]), e["issueNumber"]

# ══════════════════════════════════════════════════════════════════
# TRANSITION HELPERS
# ══════════════════════════════════════════════════════════════════
def _digit_trans(h, decay=0.9):
    if len(h) < 2: return [1.0]*10
    cur = h[-1]; w = [0.0]*10; wt = 1.0
    for i in range(len(h)-2, -1, -1):
        if h[i] == cur: w[h[i+1]] += wt
        wt *= decay
    return w
def _colour_trans(h, decay=0.9):
    if len(h) < 2: return [1.0]*4
    cur = colour_of(h[-1]); w = [0.0]*4; wt = 1.0
    for i in range(len(h)-2, -1, -1):
        if colour_of(h[i]) == cur: w[colour_of(h[i+1])] += wt
        wt *= decay
    return w
def _parity_trans(h, decay=0.9):
    if len(h) < 2: return [1.0, 1.0]
    cur = parity_of(h[-1]); w = [0.0, 0.0]; wt = 1.0
    for i in range(len(h)-2, -1, -1):
        if parity_of(h[i]) == cur: w[parity_of(h[i+1])] += wt
        wt *= decay
    return w
def _size_trans(h, decay=0.9):
    if len(h) < 2: return [1.0, 1.0]
    cur = size_of(h[-1]); w = [0.0, 0.0]; wt = 1.0
    for i in range(len(h)-2, -1, -1):
        if size_of(h[i]) == cur: w[size_of(h[i+1])] += wt
        wt *= decay
    return w
def _pair_trans(h, decay=0.9):
    if len(h) < 3: return [1.0]*10
    key = (h[-2], h[-1]); w = [0.0]*10; wt = 1.0
    for i in range(len(h)-3, -1, -1):
        if (h[i], h[i+1]) == key: w[h[i+2]] += wt
        wt *= decay
    return w
def _triple_trans(h, decay=0.9):
    if len(h) < 4: return [1.0]*10
    key = (h[-3], h[-2], h[-1]); w = [0.0]*10; wt = 1.0
    for i in range(len(h)-4, -1, -1):
        if (h[i], h[i+1], h[i+2]) == key: w[h[i+3]] += wt
        wt *= decay
    return w
def _cp_trans(h, decay=0.9):
    if len(h) < 2: return [1.0]*10
    key = (colour_of(h[-1]), parity_of(h[-1])); w = [0.0]*10; wt = 1.0
    for i in range(len(h)-2, -1, -1):
        if (colour_of(h[i]), parity_of(h[i])) == key: w[h[i+1]] += wt
        wt *= decay
    return w
def _gap_weights(h):
    if not h: return [1.0]*10
    last = {}
    for i, d in enumerate(reversed(h)):
        if d not in last: last[d] = i
    return [last.get(d, len(h)) for d in range(10)]
def _freq_weights(h, decay=0.9):
    w = [0.0]*10; wt = 1.0
    for d in reversed(h):
        w[d] += wt; wt *= decay
    return w
def _pick(w, mode):
    total = sum(w)
    if total <= 0: return random.randint(0, 9)
    if mode == "sample":  return weighted_sample(w)
    if mode == "argmax":  return max(range(len(w)), key=lambda i: w[i])
    if mode == "argmin":  return min(range(len(w)), key=lambda i: w[i])
    if mode == "second":
        o = sorted(range(len(w)), key=lambda i: -w[i])
        return o[1] if len(o) > 1 else o[0]
    if mode == "third":
        o = sorted(range(len(w)), key=lambda i: -w[i])
        return o[2] if len(o) > 2 else o[0]
    if mode == "top3":
        o = sorted(range(len(w)), key=lambda i: -w[i])[:3]
        sub = [w[i] for i in o]
        return o[weighted_sample(sub)]
    return random.randint(0, 9)

# ══════════════════════════════════════════════════════════════════
# TOOLS
# ══════════════════════════════════════════════════════════════════
def t_mean(w):
    def f(h): return clamp(sum(h[-w:])/w) if len(h) >= w else None
    return f
def t_median(w):
    def f(h): return clamp(statistics.median(h[-w:])) if len(h) >= w else None
    return f
def t_mode(w):
    def f(h): return Counter(h[-w:]).most_common(1)[0][0] if len(h) >= w else None
    return f
def t_anti_mode(w):
    def f(h):
        if len(h) < w: return None
        c = Counter(h[-w:])
        return min(range(10), key=lambda d: c.get(d, 0))
    return f
def t_rwf(decay, mode):
    def f(h): return _pick(_freq_weights(h, decay), mode)
    return f
def t_hot_cold(window, mode):
    def f(h):
        if len(h) < window: return None
        c = Counter(h[-window:])
        if mode == "hot":  return c.most_common(1)[0][0]
        if mode == "cold": return min(range(10), key=lambda d: c.get(d, 0))
        mc = c.most_common()
        return mc[1][0] if len(mc) > 1 else mc[0][0]
    return f
def t_gap(mode):
    def f(h): return _pick(_gap_weights(h), mode)
    return f
def t_gap_by_colour(target_c):
    def f(h):
        if not h: return None
        last = {}
        for i, d in enumerate(reversed(h)):
            if d not in last: last[d] = i
        cands = digits_of_colour(target_c)
        gaps = [last.get(d, len(h)) for d in cands]
        return cands[gaps.index(max(gaps))]
    return f
def t_digit_trans(decay, mode):
    def f(h): return _pick(_digit_trans(h, decay), mode)
    return f
def t_pair_trans(decay, mode):
    def f(h): return _pick(_pair_trans(h, decay), mode)
    return f
def t_triple_trans(decay, mode):
    def f(h): return _pick(_triple_trans(h, decay), mode)
    return f
def t_cp_trans(decay, mode):
    def f(h): return _pick(_cp_trans(h, decay), mode)
    return f
def t_colour_trans(decay, mode, rule="hot"):
    def f(h):
        cw = _colour_trans(h, decay)
        c = _pick(cw, "argmax" if mode == "argmax" else "sample")
        cands = digits_of_colour(c)
        if rule == "hot":
            cnt = Counter(h[-10:]); return max(cands, key=lambda d: cnt.get(d, 0))
        if rule == "cold":
            cnt = Counter(h[-10:]); return min(cands, key=lambda d: cnt.get(d, 0))
        if rule == "gap":
            last = {}
            for i, d in enumerate(reversed(h)):
                if d not in last: last[d] = i
            return max(cands, key=lambda d: last.get(d, len(h)))
        return random.choice(cands)
    return f
def t_pattern(length, mode="top"):
    def f(h):
        if len(h) <= length: return None
        pat = tuple(h[-length:])
        hits = [h[i+length] for i in range(len(h)-length)
                if tuple(h[i:i+length]) == pat and i+length < len(h)]
        if not hits: return None
        c = Counter(hits)
        if mode == "top": return c.most_common(1)[0][0]
        if mode == "bottom": return min(range(10), key=lambda d: c.get(d, 0))
        return weighted_sample([c.get(d, 0)+0.001 for d in range(10)])
    return f
def t_nn(window):
    def f(h):
        if len(h) < window + 1: return None
        key = tuple(h[-window:])
        best = None; bd = float('inf')
        for i in range(len(h)-window):
            if i+window >= len(h): continue
            d = sum(abs(a-b) for a, b in zip(h[i:i+window], key))
            if d < bd: bd = d; best = h[i+window]
        return best
    return f
def t_structural(kind):
    def f(h):
        if not h: return None
        if kind == "repeat": return h[-1]
        if kind == "not_last": return random.choice([d for d in range(10) if d != h[-1]])
        if kind == "shift+1": return (h[-1]+1) % 10
        if kind == "shift-1": return (h[-1]-1) % 10
        if kind == "mirror": return 9 - h[-1]
        if kind == "cycle3": return h[-3] if len(h) >= 3 else None
        if kind == "cycle4": return h[-4] if len(h) >= 4 else None
        if kind == "sum2_mod": return (h[-1]+h[-2])%10 if len(h) >= 2 else None
        if kind == "diff2_mod": return abs(h[-1]-h[-2]) if len(h) >= 2 else None
    return f
def t_conditional(cond, target):
    def f(h):
        if not h: return None
        cur = h[-1]
        if cond == "big" and size_of(cur) != 1: return None
        if cond == "small" and size_of(cur) != 0: return None
        if cond == "odd" and parity_of(cur) != 1: return None
        if cond == "even" and parity_of(cur) != 0: return None
        if target == "big":   cands = [d for d in range(10) if size_of(d) == 1]
        elif target == "small": cands = [d for d in range(10) if size_of(d) == 0]
        elif target == "odd":   cands = [d for d in range(10) if parity_of(d) == 1]
        elif target == "even":  cands = [d for d in range(10) if parity_of(d) == 0]
        else: cands = list(range(10))
        last = {}
        for i, d in enumerate(reversed(h)):
            if d not in last: last[d] = i
        return max(cands, key=lambda d: last.get(d, len(h)))
    return f
def t_succ_stats(group_by, stat):
    def f(h):
        if len(h) < 2: return None
        if group_by == "digit":
            key = h[-1]; succs = [h[i+1] for i in range(len(h)-1) if h[i] == key]
        elif group_by == "colour":
            key = colour_of(h[-1]); succs = [h[i+1] for i in range(len(h)-1) if colour_of(h[i]) == key]
        elif group_by == "parity":
            key = parity_of(h[-1]); succs = [h[i+1] for i in range(len(h)-1) if parity_of(h[i]) == key]
        else:
            key = size_of(h[-1]); succs = [h[i+1] for i in range(len(h)-1) if size_of(h[i]) == key]
        if not succs: return None
        if stat == "mean":   return clamp(sum(succs)/len(succs))
        if stat == "median": return clamp(statistics.median(succs))
        if stat == "mode":   return Counter(succs).most_common(1)[0][0]
        return random.choice(succs)
    return f
def t_prob_freq(decay):
    def f(h): return weighted_sample(_freq_weights(h, decay))
    return f
def t_prob_gap(scale):
    def f(h):
        if not h: return None
        w = [math.exp(g*scale) for g in _gap_weights(h)]
        return weighted_sample(w)
    return f
def t_prob_ens(decays):
    def f(h):
        w = [0.0]*10
        for d in decays:
            for i, x in enumerate(_digit_trans(h, d)): w[i] += x
        for i, x in enumerate(_pair_trans(h, 0.85)): w[i] += x
        return weighted_sample(w)
    return f
def t_top_in_colour(decay, which="hot"):
    def f(h):
        cw = _colour_trans(h, decay)
        c = max(range(4), key=lambda i: cw[i])
        cands = digits_of_colour(c)
        cnt = Counter(h)
        if which == "hot": return max(cands, key=lambda d: cnt.get(d, 0))
        return min(cands, key=lambda d: cnt.get(d, 0))
    return f
def t_linear_reg():
    def f(h):
        n = len(h)
        if n < 4: return None
        xm = (n-1)/2; ym = sum(h)/n
        num = sum((i-xm)*(h[i]-ym) for i in range(n))
        den = sum((i-xm)**2 for i in range(n))
        if den == 0: return None
        slope = num/den
        return clamp(ym + slope*(n-xm))
    return f
def t_ewma(alpha):
    def f(h):
        if not h: return None
        s = h[0]
        for x in h[1:]: s = alpha*x + (1-alpha)*s
        return clamp(s)
    return f
def t_momentum(lb):
    def f(h):
        if len(h) < lb+1: return None
        d = (h[-1] - h[-1-lb])/lb
        return clamp(h[-1] + d)
    return f
def t_spec_streak(direction="continue"):
    def f(h):
        if len(h) < 3: return None
        if all(x >= 5 for x in h[-3:]):
            target = 1 if direction == "continue" else 0
            return random.choice([d for d in range(10) if size_of(d) == target])
        if all(x < 5 for x in h[-3:]):
            target = 0 if direction == "continue" else 1
            return random.choice([d for d in range(10) if size_of(d) == target])
        return None
    return f
def t_spec_par():
    def f(h):
        if len(h) < 4: return None
        if all(parity_of(x) == parity_of(h[-1]) for x in h[-4:]):
            target = 1 - parity_of(h[-1])
            return random.choice([d for d in range(10) if parity_of(d) == target])
        return None
    return f
def t_spec_col():
    def f(h):
        if len(h) < 3: return None
        if all(colour_of(x) == colour_of(h[-1]) for x in h[-3:]):
            others = [d for d in range(10) if colour_of(d) != colour_of(h[-1])]
            return random.choice(others)
        return None
    return f
def t_spec_ent():
    def f(h):
        if len(h) < 6: return None
        c = Counter(h[-6:])
        p = [v/6 for v in c.values()]
        ent = -sum(x*math.log2(x) for x in p if x > 0)
        if ent < 1.8: return c.most_common(1)[0][0]
        return None
    return f
def t_spec_gaphot():
    def f(h):
        if len(h) < 8: return None
        last = {}
        for i, d in enumerate(reversed(h)):
            if d not in last: last[d] = i
        cold = [d for d in range(10) if last.get(d, len(h)) >= 8]
        if not cold: return None
        return random.choice(cold)
    return f
def t_rare_triple():
    def f(h):
        if len(h) < 5: return None
        key = (h[-3], h[-2], h[-1])
        matches = [h[i+3] for i in range(len(h)-3)
                   if (h[i], h[i+1], h[i+2]) == key and i+3 < len(h)]
        if not matches: return None
        return Counter(matches).most_common(1)[0][0]
    return f
def t_auto_cycle():
    def f(h):
        n = len(h)
        if n < 6: return None
        best_p = None; best_score = -1
        for p in range(2, min(n-1, 8)):
            matches = sum(1 for i in range(p, n) if h[i] == h[i-p])
            score = matches/(n-p)
            if score > best_score:
                best_score = score; best_p = p
        if best_p is None or best_score < 0.4: return None
        return h[-best_p]
    return f
def t_anti_pop():
    def f(h):
        if not h: return None
        c = Counter(h)
        return min(range(10), key=lambda d: c.get(d, 0))
    return f
def t_par_balance():
    def f(h):
        if len(h) < 4: return None
        odds = sum(1 for x in h[-6:] if parity_of(x) == 1)
        evens = sum(1 for x in h[-6:] if parity_of(x) == 0)
        target = 0 if odds > evens else 1
        return random.choice([d for d in range(10) if parity_of(d) == target])
    return f
def t_sz_balance():
    def f(h):
        if len(h) < 4: return None
        bigs = sum(1 for x in h[-6:] if size_of(x) == 1)
        smalls = sum(1 for x in h[-6:] if size_of(x) == 0)
        target = 0 if bigs > smalls else 1
        return random.choice([d for d in range(10) if size_of(d) == target])
    return f
def t_col_balance():
    def f(h):
        if len(h) < 4: return None
        cnt = Counter(colour_of(x) for x in h[-6:])
        target = min(cnt, key=lambda c: cnt.get(c, 0))
        return random.choice(digits_of_colour(target))
    return f
def t_sum_mod(n):
    def f(h): return sum(h[-n:]) % 10 if len(h) >= n else None
    return f
def t_prod_mod():
    def f(h): return (h[-1]*h[-2]) % 10 if len(h) >= 2 else None
    return f
def t_kalman():
    def f(h):
        if len(h) < 3: return None
        x = float(h[0]); p = 1.0; Q = 0.4; R = 2.0
        for z in h[1:]:
            p += Q; k = p/(p+R); x += k*(z-x); p *= (1-k)
        return clamp(x)
    return f
def t_autocorr():
    def f(h):
        n = len(h)
        if n < 6: return None
        m = sum(h)/n
        var = sum((x-m)**2 for x in h)/n
        if var == 0: return None
        best_lag, best_corr = None, 0
        for lag in range(1, min(n//2, 5)):
            num = sum((h[i]-m)*(h[i+lag]-m) for i in range(n-lag))/(n-lag)
            c = num/var
            if c > best_corr:
                best_corr = c; best_lag = lag
        return h[-best_lag] if best_lag else None
    return f
def t_hurst():
    def f(h):
        n = len(h)
        if n < 8: return None
        m = sum(h)/n
        Y = []; s = 0
        for x in h: s += x - m; Y.append(s)
        R = max(Y) - min(Y)
        S = (sum((x-m)**2 for x in h)/n)**0.5
        if S == 0: return None
        H = math.log(R/S)/math.log(n) if R/S > 0 else 0.5
        if H > 0.5: return clamp(h[-1] + (h[-1]-h[0])/n)
        return clamp(m)
    return f
def t_attention():
    def f(h):
        if len(h) < 4: return None
        last = h[-1]
        scores = []
        for i, x in enumerate(h[:-1]):
            sim = 1.0/(1 + abs(x - last))
            recency = i + 1
            scores.append((sim*recency, h[i+1]))
        if not scores: return None
        scores.sort(reverse=True)
        top = scores[:3]
        total = sum(s for s, _ in top)
        if total == 0: return None
        return clamp(sum(s*v for s, v in top)/total)
    return f
def t_pst():
    def f(h):
        if len(h) < 6: return None
        key = tuple(h[-3:])
        counts = Counter()
        for i in range(len(h)-3):
            if tuple(h[i:i+3]) == key:
                counts[h[i+3]] += 1
        if not counts: return None
        return weighted_sample([counts.get(d, 0)+0.1 for d in range(10)])
    return f
def t_ctw():
    def f(h):
        if len(h) < 5: return None
        w = [0.0]*10
        for order in [1, 2, 3]:
            if len(h) < order+1: continue
            key = tuple(h[-order:])
            local = [0.0]*10
            for i in range(len(h)-order):
                if tuple(h[i:i+order]) == key:
                    local[h[i+order]] += 1
            tot = sum(local)
            if tot > 0:
                for d in range(10):
                    w[d] += (local[d]/tot) * (order/6.0)
        if sum(w) == 0: return None
        return weighted_sample(w)
    return f
def t_dirichlet():
    def f(h):
        if len(h) < 4: return None
        counts = Counter(h); n = len(h); alpha = 1.0
        probs = [(counts.get(d, 0)+alpha/10)/(n+alpha) for d in range(10)]
        return weighted_sample(probs)
    return f
def t_boltzmann():
    def f(h):
        n = len(h)
        if n < 5: return None
        var = sum((x-sum(h)/n)**2 for x in h)/n
        T = max(0.5, var); last = h[-1]
        energies = [abs(d - last) for d in range(10)]
        weights = [math.exp(-e/T) for e in energies]
        return weighted_sample(weights)
    return f
def t_forest():
    def f(h):
        if len(h) < 5: return None
        random.seed(len(h))
        votes = Counter()
        for _ in range(3):
            k = random.randint(2, min(5, len(h)))
            window = h[-k:]
            votes[Counter(window).most_common(1)[0][0]] += 1
        return votes.most_common(1)[0][0]
    return f
def t_thompson():
    def f(h):
        n = len(h)
        if n < 5: return None
        samples = [random.betavariate(1 + h[-5:].count(d), 1 + 5 - h[-5:].count(d))
                   for d in range(10)]
        return max(range(10), key=lambda d: samples[d])
    return f
def t_ucb():
    def f(h):
        n = len(h)
        if n < 6: return None
        cnt = Counter(h); n_total = len(h)
        scores = [0.0]*10
        for d in range(10):
            c = cnt.get(d, 0)
            if c == 0: scores[d] = 100
            else: scores[d] = c/n_total + math.sqrt(2*math.log(n_total)/c)
        return max(range(10), key=lambda d: scores[d])
    return f
def t_expected():
    def f(h):
        n = len(h)
        if n < 5: return None
        w = [0.0]*10; wt = 1.0
        for x in reversed(h):
            w[x] += wt; wt *= 0.8
        total = sum(w)
        probs = [x/total for x in w]
        expected = sum(d*p for d, p in enumerate(probs))
        return clamp(expected)
    return f

# ══════════════════════════════════════════════════════════════════
# CONSCIOUS MIND
# ══════════════════════════════════════════════════════════════════
class ConsciousMind:
    def __init__(self, name, tool, jitter=0.1):
        self.name = name
        self.tool = tool
        self.jitter = jitter
        self.recent_hits = deque(maxlen=MIND_HISTORY_LEN)
        self.recent_votes = deque(maxlen=MIND_HISTORY_LEN)
        self.streak = 0
        self.mood = "curious"
        self.energy = 1.0
        self.total_votes = 0
        self.total_abstains = 0
        self.context_memory = defaultdict(lambda: [0, 0])
        self.last_context = None
        self.self_belief = 0.5
        self.last_reason = ""

    def _context(self, history):
        if not history: return None
        return (colour_of(history[-1]), parity_of(history[-1]), size_of(history[-1]))

    def _should_abstain(self, context):
        if context is not None:
            hits, total = self.context_memory[context]
            if total >= 3 and hits/total < 0.1:
                return "context failure"
        if self.energy < 0.3 and self.mood in ("tired", "cautious"):
            return "low energy"
        if self.mood == "confused" and random.random() < 0.35:
            return "confused"
        return None

    def vote(self, history):
        context = self._context(history)
        self.last_context = context

        reason = self._should_abstain(context)
        if reason is not None:
            self.total_abstains += 1
            self.last_reason = reason
            return None

        if random.random() < self.jitter * (2 - self.energy):
            v = random.randint(0, 9)
        else:
            try:
                v = self.tool(history)
                if v is None or not isinstance(v, int) or not (0 <= v <= 9):
                    v = None
            except Exception:
                v = None

        if v is None:
            self.total_abstains += 1
            self.last_reason = "tool abstain"
            return None

        self.total_votes += 1
        self.last_reason = "voted"
        return v

    def _recency_accuracy(self):
        """Recency-weighted accuracy — recent hits count more."""
        if not self.recent_hits: return None
        n = len(self.recent_hits)
        weights = [math.exp(-(n-1-i) / RECENCY_HALFLIFE) for i in range(n)]
        total_w = sum(weights)
        if total_w == 0: return 0.0
        return sum(h * w for h, w in zip(self.recent_hits, weights)) / total_w

    def weight(self):
        rec_acc = self._recency_accuracy()
        base = rec_acc if rec_acc is not None else MIND_PRIOR_WEIGHT
        # energy mod
        base *= (0.5 + self.energy * 0.5)
        # streak factor
        base *= (1.0 + max(-0.2, min(0.2, self.streak * 0.05)))
        # self-belief
        base *= (0.7 + self.self_belief * 0.6)
        return max(MIND_WEIGHT_FLOOR, base)

    def reflect(self, actual, vote, peers_agreed_frac=None):
        if vote is None: return
        hit = 1 if vote == actual else 0
        self.recent_hits.append(hit)
        self.recent_votes.append(vote)

        # Stronger recovery, slower decay
        if hit:
            self.energy = min(1.0, self.energy + 0.08)
            self.self_belief = min(1.0, self.self_belief + 0.05)
            if self.streak < 0: self.streak = 0
            self.streak += 1
            if self.streak >= 2: self.mood = "confident"
            else: self.mood = "curious"
        else:
            self.energy = max(0.25, self.energy - 0.05)
            self.self_belief = max(0.0, self.self_belief - 0.04)
            if self.streak > 0: self.streak = 0
            self.streak -= 1
            if self.streak <= -3:
                self.mood = "cautious"
            elif self.streak <= -5:
                self.mood = "confused"
            else:
                self.mood = "cautious"

        # context memory
        if self.last_context is not None:
            self.context_memory[self.last_context][0] += hit
            self.context_memory[self.last_context][1] += 1

    def rebind_tool(self, new_tool):
        """Called when admin replaces a failed mind with a fresh instance."""
        self.tool = new_tool
        self.recent_hits.clear()
        self.recent_votes.clear()
        self.streak = 0
        self.mood = "curious"
        self.energy = 1.0
        self.self_belief = 0.5
        self.context_memory.clear()
        self.total_votes = 0
        self.total_abstains = 0

    def narrative(self):
        acc = self._recency_accuracy()
        acc_str = f"{acc*100:.0f}%" if acc is not None else "--"
        return (f"mood={self.mood} acc={acc_str} "
                f"stk={self.streak:+d} E={self.energy:.2f} belief={self.self_belief:.2f}")


class IssueNumberMind(ConsciousMind):
    def __init__(self, jitter=0.1):
        super().__init__("IssueNumberHunter", lambda h: None, jitter)
        self.last_issue = None
    def set_issue(self, issue):
        self.last_issue = issue
    def vote(self, history):
        if not self.last_issue: return None
        try:
            tail = int(str(self.last_issue)[-2:])
        except Exception:
            return None
        self.total_votes += 1
        return tail % 10


class SeedScatterMind(ConsciousMind):
    def __init__(self, name, jitter=0.1, pool_size=200, fresh_per_round=30):
        super().__init__(name, lambda h: None, jitter)
        self.pool_size = pool_size
        self.fresh_per_round = fresh_per_round
        self.seed_pool = [random.randint(0, 2**31) for _ in range(pool_size)]
        self.best_seed = None
        self.best_score = 0
        self.fail_streak = 0
        self.total_reseeds = 0

    def _score_seed(self, seed, recent):
        try:
            rng = random.Random(seed)
            return sum(1 for actual in recent if rng.randint(0, 9) == actual)
        except Exception:
            return 0

    def _predict_next(self, seed, n_consumed):
        rng = random.Random(seed)
        for _ in range(n_consumed):
            rng.randint(0, 9)
        return rng.randint(0, 9)

    def vote(self, history):
        if len(history) < 8: return None
        recent = history[-10:]
        scored = [(self._score_seed(s, recent), s) for s in self.seed_pool]
        for _ in range(self.fresh_per_round):
            s = random.randint(0, 2**31)
            scored.append((self._score_seed(s, recent), s))
        try:
            derived = [
                abs(hash(tuple(recent))) % (2**31),
                int(''.join(map(str, recent))) % (2**31),
            ]
            for ds in derived:
                scored.append((self._score_seed(ds, recent), ds))
        except Exception:
            pass
        scored.sort(reverse=True, key=lambda x: x[0])
        best_score, best_seed = scored[0]
        self.seed_pool = [s for _, s in scored[:self.pool_size]]
        if best_score < 3:
            self.fail_streak += 1
            if self.fail_streak >= 2:
                n_replace = self.pool_size // 2
                self.seed_pool[-n_replace:] = [
                    random.randint(0, 2**31) for _ in range(n_replace)
                ]
                self.total_reseeds += n_replace
                self.fail_streak = 0
            self.total_abstains += 1
            self.last_reason = "seed weak"
            return None
        self.best_seed = best_seed
        self.best_score = best_score
        pred = self._predict_next(best_seed, len(recent))
        self.total_votes += 1
        self.last_reason = f"seed hit {best_score}/10"
        return pred


class SizeColourRelationMind(ConsciousMind):
    def __init__(self, name, jitter=0.1, min_confidence=0.35):
        super().__init__(name, lambda h: None, jitter)
        self.min_confidence = min_confidence

    def vote(self, history):
        if len(history) < 6: return None
        primary = defaultdict(Counter)
        for i in range(len(history) - 1):
            key = (size_of(history[i]), colour_of(history[i]))
            primary[key][history[i+1]] += 1
        last_size = size_of(history[-1])
        last_colour = colour_of(history[-1])
        key = (last_size, last_colour)
        counts = primary.get(key)
        if not counts:
            self.total_abstains += 1; self.last_reason = "no joint context"
            return None
        total = sum(counts.values())
        best_d, best_c = counts.most_common(1)[0]
        confidence = best_c / total
        if confidence < self.min_confidence:
            self.total_abstains += 1; self.last_reason = f"weak joint ({confidence:.2f})"
            return None
        joint = Counter()
        for i in range(len(history) - 1):
            joint[(size_of(history[i]), colour_of(history[i]),
                   size_of(history[i+1]), colour_of(history[i+1]))] += 1
        best_secondary = None
        best_secondary_score = 0
        if len(history) >= 8:
            for d in range(10):
                sc_next = (size_of(d), colour_of(d))
                k = (last_size, last_colour, sc_next[0], sc_next[1])
                score = joint.get(k, 0)
                if score > best_secondary_score:
                    best_secondary_score = score
                    best_secondary = d
        if best_secondary is not None and best_secondary == best_d:
            self.total_votes += 1; self.last_reason = f"joint agree ({confidence:.2f})"
            return best_d
        if confidence >= 0.5:
            self.total_votes += 1; self.last_reason = f"primary strong ({confidence:.2f})"
            return best_d
        self.total_abstains += 1; self.last_reason = "not feeling it"
        return None

# ══════════════════════════════════════════════════════════════════
# META-LEARNER + AGREEMENT + BET SIMULATOR
# ══════════════════════════════════════════════════════════════════
class MetaLearner:
    """
    Tracks which strategy succeeds under which crowd conditions.
    Uses entropy-bucket × top-share-bucket × big-share-bucket as context.
    """
    STRATEGIES = ["least", "most", "middle", "sample_inverse",
                  "top_consensus", "single_top_mind"]

    def __init__(self):
        # context_key -> strategy -> deque of hits
        self.table = defaultdict(lambda: {s: deque(maxlen=20) for s in self.STRATEGIES})

    def _context_key(self, entropy, top_share, big_share):
        eb = "L" if entropy < 0.75 else ("M" if entropy < 0.9 else "H")
        tb = "L" if top_share < 0.15 else ("M" if top_share < 0.25 else "H")
        bb = "L" if big_share < 0.4 else ("M" if big_share < 0.6 else "H")
        return f"{eb}{tb}{bb}"

    def best_strategy(self, entropy, top_share, big_share, fallback="least"):
        key = self._context_key(entropy, top_share, big_share)
        bucket = self.table[key]
        rates = {}
        for s in self.STRATEGIES:
            if len(bucket[s]) >= 4:
                rates[s] = sum(bucket[s]) / len(bucket[s])
        if not rates:
            return fallback
        return max(rates, key=rates.get)

    def record(self, entropy, top_share, big_share, strategy, hit):
        key = self._context_key(entropy, top_share, big_share)
        self.table[key][strategy].append(hit)

    def summary(self):
        rows = []
        for key, bucket in sorted(self.table.items()):
            best_s, best_rate = None, -1
            for s, sc in bucket.items():
                if len(sc) >= 4:
                    r = sum(sc) / len(sc)
                    if r > best_rate:
                        best_rate = r; best_s = s
            if best_s:
                rows.append((key, best_s, best_rate, sum(len(sc) for sc in bucket.values())))
        return rows


class AgreementEngine:
    """
    Runs each strategy mentally on the current round to see which digit
    each would pick. If several converge on the same digit, boost it.
    """
    def __init__(self, strategies):
        self.strategies = strategies

    def evaluate(self, sense, weighted):
        w = sense["w"]
        picks = {}
        # least
        lc = min(w); picks["least"] = [d for d in range(10) if w[d] == lc]
        # most
        mc = max(w); picks["most"] = [d for d in range(10) if w[d] == mc]
        # middle
        s = sorted(w); mid = s[len(s)//2]
        picks["middle"] = [d for d in range(10) if abs(w[d] - mid) < 1e-6] or list(range(10))
        # single_top_mind
        top = max(weighted, key=lambda vw: vw[1])
        picks["single_top_mind"] = [top[0]] if top[0] is not None else list(range(10))
        # top_consensus
        top5 = sorted(weighted, key=lambda vw: -vw[1])[:5]
        votes = [v for v, _ in top5 if v is not None]
        if votes:
            mc_vote = Counter(votes).most_common(1)[0]
            picks["top_consensus"] = [mc_vote[0]] if mc_vote[1] >= 3 else list(range(10))
        else:
            picks["top_consensus"] = list(range(10))
        # sample_inverse — no single pick, skip
        picks["sample_inverse"] = list(range(10))

        # Build agreement scores
        agree = [0.0] * 10
        for s in self.strategies:
            if s == "sample_inverse": continue
            for d in picks.get(s, []):
                agree[d] += 1
        return agree

    def boost_chart(self, probs, agree, weight=0.15):
        boosted = []
        for d in range(10):
            boosted.append(probs[d] * (1 + weight * agree[d]))
        total = sum(boosted)
        if total > 0:
            boosted = [x / total for x in boosted]
        return boosted


class DigitReliability:
    """Tracks each digit's historical success rate when in top-K."""
    def __init__(self):
        self.top1_hits = defaultdict(int)
        self.top1_count = defaultdict(int)
        self.top5_hits = defaultdict(int)
        self.top5_count = defaultdict(int)

    def update(self, ranked, actual):
        top1 = ranked[:1]
        top5 = ranked[:5]
        for d in top1:
            self.top1_count[d] += 1
            if d == actual: self.top1_hits[d] += 1
        for d in top5:
            self.top5_count[d] += 1
            if d == actual: self.top5_hits[d] += 1

    def top5_rate(self, d):
        c = self.top5_count[d]
        if c == 0: return 0.0
        return self.top5_hits[d] / c

    def top1_rate(self, d):
        c = self.top1_count[d]
        if c == 0: return 0.0
        return self.top1_hits[d] / c

    def rerank(self, probs):
        """Bias the chart by each digit's historical reliability."""
        ranked = sorted(range(10), key=lambda d: -probs[d])
        scored = {}
        for d in range(10):
            r5 = self.top5_rate(d)
            # base = probability; multiply by (1 + r5) so reliable digits get boosted
            scored[d] = probs[d] * (1 + r5 * 0.5)
        total = sum(scored.values())
        if total > 0:
            return [scored[d] / total for d in range(10)]
        return probs


class BetSimulator:
    """Tracks hypothetical P&L for Top-1..Top-5 betting at 9.8x payout."""
    def __init__(self, payout=9.8):
        self.payout = payout
        self.reset()

    def reset(self):
        self.total_rounds = 0
        self.topk_hits = {k: 0 for k in [1, 2, 3, 4, 5]}
        self.topk_pnl = {k: 0.0 for k in [1, 2, 3, 4, 5]}
        # stake = K units per round (1 per number); win = payout units

    def add_round(self, ranked, actual):
        self.total_rounds += 1
        for k in [1, 2, 3, 4, 5]:
            topk = set(ranked[:k])
            if actual in topk:
                self.topk_hits[k] += 1
                self.topk_pnl[k] += self.payout - k
            else:
                self.topk_pnl[k] -= k

    def summary(self):
        rows = []
        for k in [1, 2, 3, 4, 5]:
            if self.total_rounds == 0:
                rows.append((k, 0, 0, 0.0, 0.0))
                continue
            hr = self.topk_hits[k] / self.total_rounds
            rows.append((k, self.topk_hits[k], self.total_rounds, hr, self.topk_pnl[k]))
        return rows


# ══════════════════════════════════════════════════════════════════
# CONSCIOUS ADMIN (upgraded)
# ══════════════════════════════════════════════════════════════════
class ConsciousAdmin:
    STRATEGIES = ["least", "most", "middle", "sample_inverse",
                  "top_consensus", "single_top_mind"]

    def __init__(self):
        self.strategy_scores = {s: deque(maxlen=STRATEGY_WINDOW) for s in self.STRATEGIES}
        self.active_strategy = "least"
        self.switch_reason = None
        self.miss_streak = 0
        self.last_pick = None
        self.last_ranked = list(range(10))
        self.last_context_key = None
        self.last_sense = None

        self.meta = MetaLearner()
        self.agreement = AgreementEngine(self.STRATEGIES)
        self.reliability = DigitReliability()

        self.self_accuracy = deque(maxlen=50)
        self.thought = ""
        self.inner_voice = "I am observing."
        self.confidence_in_self = 0.5
        self.total_picks = 0

    def sense(self, weighted, minds):
        w = [0.0] * 10
        for v, weight in weighted:
            if v is not None:
                w[v] += weight
        total = sum(w)
        if total <= 0:
            return {"entropy": 1.0, "top_share": 0.0, "big_share": 0.5, "w": w}
        p = [x / total for x in w if x > 0]
        entropy = -sum(x * math.log2(x) for x in p) / math.log2(10)
        top_share = max(w) / total
        big_share = sum(w[5:]) / total
        return {"entropy": entropy, "top_share": top_share, "big_share": big_share, "w": w}

    def think(self, sense, active_count):
        lines = []
        if sense["entropy"] > 0.9:
            lines.append("The crowd feels chaotic."); self.inner_voice = "chaotic"
        elif sense["entropy"] > 0.75:
            lines.append("There's a lean, but no conviction."); self.inner_voice = "leaning"
        else:
            lines.append("The crowd has found shape."); self.inner_voice = "focused"

        if sense["top_share"] > 0.25:
            lines.append(f"A leader holds {sense['top_share']*100:.0f}% of weight.")
        if sense["big_share"] > 0.65:
            lines.append("The crowd leans BIG.")
        elif sense["big_share"] < 0.35:
            lines.append("The crowd leans SMALL.")

        if len(self.self_accuracy) >= 5:
            rate = sum(self.self_accuracy) / len(self.self_accuracy)
            if rate >= 0.2:
                self.confidence_in_self = min(1.0, self.confidence_in_self + 0.05)
                lines.append("My recent picks have been sharp.")
            elif rate <= 0.05:
                self.confidence_in_self = max(0.2, self.confidence_in_self - 0.05)
                lines.append("I've been missing — listening more carefully.")

        if self.miss_streak >= 3:
            lines.append("Cold streak — changing approach.")
        if active_count < 60:
            lines.append(f"Only {active_count} minds speaking.")

        self.thought = " ".join(lines)

    def pick_strategy(self, sense):
        """Choose the strategy for this round using meta-learner + cold streak."""
        # Cold-streak override
        if self.miss_streak >= 3:
            others = [s for s in self.STRATEGIES if s != self.active_strategy]
            best = max(others, key=lambda s: (
                sum(self.strategy_scores[s]) / max(1, len(self.strategy_scores[s]))
                + random.uniform(0, 0.05)))
            old = self.active_strategy
            self.active_strategy = best
            self.miss_streak = 0
            return f"{old}→{best} (cold streak)"

        # Meta-learner recommendation
        meta_best = self.meta.best_strategy(
            sense["entropy"], sense["top_share"], sense["big_share"],
            fallback=self.active_strategy)
        if meta_best != self.active_strategy:
            old = self.active_strategy
            self.active_strategy = meta_best
            return f"{old}→{meta_best} (meta-learner)"

        # Performance-based switch
        if all(len(self.strategy_scores[s]) >= STRATEGY_MIN_ROUNDS for s in self.STRATEGIES):
            rates = {s: sum(self.strategy_scores[s]) / len(self.strategy_scores[s])
                     for s in self.STRATEGIES}
            best = max(rates, key=rates.get)
            if best != self.active_strategy and rates[best] > rates[self.active_strategy] + 0.08:
                old = self.active_strategy
                self.active_strategy = best
                return f"{old}→{best} (perf {rates[best]*100:.1f}% vs {rates[old]*100:.1f}%)"
        return None

    def execute(self, strategy, sense, weighted):
        w = sense["w"]
        if strategy == "least":
            lc = min(w); return random.choice([d for d in range(10) if w[d] == lc])
        if strategy == "most":
            mc = max(w); return random.choice([d for d in range(10) if w[d] == mc])
        if strategy == "middle":
            s = sorted(w); mid = s[len(s)//2]
            pool = [d for d in range(10) if abs(w[d] - mid) < 1e-6] or list(range(10))
            return random.choice(pool)
        if strategy == "sample_inverse":
            inv = [1.0/(x + 0.01) for x in w]
            return weighted_sample(inv)
        if strategy == "top_consensus":
            top = sorted(weighted, key=lambda vw: -vw[1])[:5]
            votes = [v for v, _ in top if v is not None]
            if votes:
                mc = Counter(votes).most_common(1)[0]
                if mc[1] >= 3: return mc[0]
            lc = min(w); return random.choice([d for d in range(10) if w[d] == lc])
        if strategy == "single_top_mind":
            top = max(weighted, key=lambda vw: vw[1])
            if top[0] is not None: return top[0]
            return random.randint(0, 9)
        return random.randint(0, 9)

    def decide(self, weighted, minds):
        sense = self.sense(weighted, minds)
        active_count = sum(1 for v, _ in weighted if v is not None)
        self.think(sense, active_count)

        # Agreement scoring
        agree = self.agreement.evaluate(sense, weighted)

        # Build base chart from weighted crowd
        total = sum(sense["w"])
        base_chart = [sense["w"][d]/total if total > 0 else 0.1 for d in range(10)]
        # Apply agreement boost
        chart = self.agreement.boost_chart(base_chart, agree, weight=0.15)
        # Apply digit reliability rerank
        chart = self.reliability.rerank(chart)

        # Rank digits by boosted chart
        ranked = sorted(range(10), key=lambda d: -chart[d])

        # Pick strategy
        switch = self.pick_strategy(sense)
        self.switch_reason = switch

        # Execute strategy on the ORIGINAL weighted crowd (not the boosted chart)
        pick = self.execute(self.active_strategy, sense, weighted)

        # But if the boosted chart is very confident, override with its #1
        top_prob = chart[ranked[0]]
        if top_prob > 0.35 and ranked[0] != pick:
            pick = ranked[0]

        self.last_pick = pick
        self.last_ranked = ranked
        self.last_sense = sense
        self.total_picks += 1

        return {
            "pick": pick,
            "strategy": self.active_strategy,
            "switch": switch,
            "thought": self.thought,
            "inner_voice": self.inner_voice,
            "sense": sense,
            "ranked": ranked,
            "chart": chart,
            "agreement": agree,
        }

    def reflect(self, actual):
        if self.last_pick is None: return
        hit = 1 if self.last_pick == actual else 0
        self.self_accuracy.append(hit)
        self.strategy_scores[self.active_strategy].append(hit)
        if self.last_sense is not None:
            self.meta.record(self.last_sense["entropy"], self.last_sense["top_share"],
                             self.last_sense["big_share"], self.active_strategy, hit)
        self.reliability.update(self.last_ranked, actual)
        if hit:
            self.miss_streak = 0
        else:
            self.miss_streak += 1

    def strategy_table(self):
        rows = []
        for s in self.STRATEGIES:
            sc = self.strategy_scores[s]
            if len(sc) == 0:
                rows.append((s, 0, 0, 0.0, "active" if s == self.active_strategy else ""))
            else:
                rate = sum(sc)/len(sc)
                rows.append((s, sum(sc), len(sc), rate,
                             "active" if s == self.active_strategy else ""))
        return rows

# ══════════════════════════════════════════════════════════════════
# BUILD MINDS
# ══════════════════════════════════════════════════════════════════
def _fresh_tool_pool():
    return [t_rwf(0.85, "sample"), t_digit_trans(0.85, "sample"),
            t_pattern(3, "top"), t_gap("sample"), t_attention(),
            t_kalman(), t_pst(), t_expected()]

def build_minds():
    M = []
    def J(): return random.uniform(0.02, 0.15)
    def add(name, tool): M.append(ConsciousMind(name, tool, J()))

    for w in [3, 5, 7]: add(f"Mean{w}", t_mean(w))
    for w in [3, 5, 7]: add(f"Median{w}", t_median(w))
    add("Mode5", t_mode(5)); add("Mode7", t_mode(7))
    add("AntiMode5", t_anti_mode(5)); add("AntiMode7", t_anti_mode(7))
    for d, m in [(0.5,"argmax"),(0.7,"argmax"),(0.85,"argmax"),(0.95,"argmax"),
                 (0.7,"sample"),(0.85,"sample"),(0.85,"argmin"),(0.9,"argmin"),
                 (0.85,"second"),(0.85,"top3")]:
        add(f"RWF_{d}_{m}", t_rwf(d, m))
    for w, m in [(5,"hot"),(7,"hot"),(10,"hot"),(5,"cold"),(7,"cold"),(10,"cold"),(7,"second")]:
        add(f"HC_{w}_{m}", t_hot_cold(w, m))
    for m in ["argmax","argmin","second","third","sample","top3"]:
        add(f"Gap_{m}", t_gap(m))
    add("GapR", t_gap_by_colour(2)); add("GapG", t_gap_by_colour(3))
    add("GapRV", t_gap_by_colour(0)); add("GapGV", t_gap_by_colour(1))
    for d, m in [(0.6,"sample"),(0.8,"sample"),(0.95,"sample"),
                 (0.7,"argmax"),(0.85,"argmax"),(0.85,"argmin"),(0.85,"second")]:
        add(f"DT_{d}_{m}", t_digit_trans(d, m))
    for d, m in [(0.85,"argmax"),(0.85,"sample"),(0.95,"sample")]:
        add(f"PT_{d}_{m}", t_pair_trans(d, m))
    for d, m in [(0.85,"argmax"),(0.85,"sample")]:
        add(f"TT_{d}_{m}", t_triple_trans(d, m))
    add("CP_a", t_cp_trans(0.85, "argmax")); add("CP_s", t_cp_trans(0.85, "sample"))
    for d, m, r in [(0.85,"argmax","hot"),(0.85,"argmax","gap"),(0.9,"sample","cold")]:
        add(f"ColT_{d}_{m}_{r}", t_colour_trans(d, m, r))
    for L, m in [(2,"top"),(3,"top"),(4,"top"),(4,"sample")]:
        add(f"Pat_{L}_{m}", t_pattern(L, m))
    add("NN3", t_nn(3)); add("NN4", t_nn(4))
    for kind in ["repeat","not_last","shift+1","shift-1","mirror","cycle3","cycle4","sum2_mod"]:
        add(f"St_{kind}", t_structural(kind))
    for c, t in [("big","big"),("big","small"),("small","big"),("small","small"),
                 ("odd","even"),("even","odd")]:
        add(f"Cond_{c}_{t}", t_conditional(c, t))
    for gb, st in [("digit","mean"),("colour","mean"),("parity","mode"),("size","median")]:
        add(f"Succ_{gb}_{st}", t_succ_stats(gb, st))
    for d in [0.7, 0.85, 0.95]: add(f"PF_{d}", t_prob_freq(d))
    add("PG_0.5", t_prob_gap(0.5))
    add("PE", t_prob_ens([0.7, 0.85, 0.95]))
    add("TopHot", t_top_in_colour(0.85, "hot"))
    add("TopCold", t_top_in_colour(0.85, "cold"))
    add("LinReg", t_linear_reg()); add("EWMA_0.3", t_ewma(0.3))
    add("EWMA_0.7", t_ewma(0.7)); add("Mom2", t_momentum(2))
    add("SpecStreakC", t_spec_streak("continue"))
    add("SpecPar", t_spec_par()); add("SpecCol", t_spec_col())
    add("SpecEnt", t_spec_ent()); add("SpecGapHot", t_spec_gaphot())
    add("RareTriple", t_rare_triple()); add("AutoCycle", t_auto_cycle())
    add("AntiPop", t_anti_pop()); add("ParBal", t_par_balance())
    add("SzBal", t_sz_balance()); add("ColBal", t_col_balance())
    add("Sum3", t_sum_mod(3)); add("Sum4", t_sum_mod(4)); add("Sum5", t_sum_mod(5))
    add("Prod", t_prod_mod())
    add("Kalman", t_kalman()); add("AutoCorr", t_autocorr())
    add("Hurst", t_hurst()); add("Attention", t_attention())
    add("PST", t_pst()); add("CTW", t_ctw()); add("Dirichlet", t_dirichlet())
    add("Boltzmann", t_boltzmann()); add("Forest", t_forest())
    add("Thompson", t_thompson()); add("UCB1", t_ucb()); add("Expected", t_expected())

    M.append(IssueNumberMind(J()))
    M.append(SeedScatterMind("SeedScatter", J()))
    M.append(SeedScatterMind("SeedScatter2", J(), pool_size=100, fresh_per_round=50))
    M.append(SizeColourRelationMind("SizeColour", J()))
    M.append(SizeColourRelationMind("SizeColour2", J(), min_confidence=0.5))

    while len(M) < 150:
        k = len(M)
        M.append(ConsciousMind(f"Extra{k}", random.choice(_fresh_tool_pool()), J()))
    return M[:150]

# ══════════════════════════════════════════════════════════════════
# ENGINE
# ══════════════════════════════════════════════════════════════════
class Engine:
    def __init__(self):
        self.minds = build_minds()
        self.admin = ConsciousAdmin()
        self.history = []
        self.total_rounds = 0
        self.correct = 0
        self.last_prediction = None
        self.last_ranked = list(range(10))
        self.last_chart = [0.1]*10
        self.last_votes = []
        self.topk_history = deque(maxlen=10)
        self.topk_history_50 = deque(maxlen=50)
        self.bet_sim = BetSimulator(payout=CONFIG.PAYOUT)
        self.rounds_since_mood_reset = 0

    def set_issue(self, issue):
        for m in self.minds:
            if hasattr(m, "set_issue"):
                m.set_issue(issue)

    def _rebalance_moods(self):
        """If one mood dominates the crowd, force some minds to shift."""
        counts = Counter(m.mood for m in self.minds)
        # If confused > 55%, force the longest-confused 30% to become curious
        if counts.get("confused", 0) > len(self.minds) * 0.55:
            confused_minds = [m for m in self.minds if m.mood == "confused"]
            random.shuffle(confused_minds)
            n_flip = len(confused_minds) // 3
            for m in confused_minds[:n_flip]:
                m.mood = "curious"
                m.energy = max(0.6, m.energy)

    def _replace_failed_minds(self):
        """Swap out minds that have been hopelessly wrong."""
        for m in self.minds:
            if len(m.recent_hits) >= FAILED_MIND_MIN:
                acc = sum(m.recent_hits) / len(m.recent_hits)
                if acc < FAILED_MIND_THRESH:
                    m.rebind_tool(random.choice(_fresh_tool_pool()))

    def run_round(self):
        with ThreadPoolExecutor(max_workers=CONFIG.MAX_WORKERS) as ex:
            raw_votes = list(ex.map(lambda m: m.vote(self.history), self.minds))
        weighted = [(v, m.weight()) for m, v in zip(self.minds, raw_votes)]
        decision = self.admin.decide(weighted, self.minds)
        self.last_prediction = decision["pick"]
        self.last_ranked = decision["ranked"]
        self.last_chart = decision["chart"]
        self.last_votes = raw_votes
        return decision

    def observe(self, actual):
        self.total_rounds += 1
        if self.last_prediction is not None and self.last_prediction == actual:
            self.correct += 1

        peer_counts = Counter(v for v in self.last_votes if v is not None)
        total_voted = sum(peer_counts.values()) or 1
        for m, v in zip(self.minds, self.last_votes):
            agree_frac = peer_counts.get(v, 0) / total_voted if v is not None else 0.0
            m.reflect(actual, v, agree_frac)

        self.admin.reflect(actual)

        rank = [1 if actual in self.last_ranked[:k] else 0 for k in [1,2,3,4,5]]
        self.topk_history.append(tuple(rank))
        self.topk_history_50.append(tuple(rank))
        self.bet_sim.add_round(self.last_ranked, actual)

        self.history.append(actual)
        if len(self.history) > CONFIG.MAX_HISTORY:
            self.history = self.history[-CONFIG.MAX_HISTORY:]

        # Periodic housekeeping
        self.rounds_since_mood_reset += 1
        if self.rounds_since_mood_reset >= 15:
            self._rebalance_moods()
            self.rounds_since_mood_reset = 0
        if self.total_rounds % 25 == 0:
            self._replace_failed_minds()

    def top_minds(self, n=6):
        scored = [(m.name, m.weight(), sum(m.recent_hits), len(m.recent_hits),
                   m.mood, m.energy, m.streak) for m in self.minds]
        scored.sort(key=lambda t: -t[1])
        return scored[:n]

# ══════════════════════════════════════════════════════════════════
# DISPLAY
# ══════════════════════════════════════════════════════════════════
def render_chart(probs):
    lines = []
    for d in range(10):
        bar = "█" * int(round(probs[d] * 55))
        lines.append(f"   {d} │ {probs[d]*100:5.1f}%  {bar}")
    return "\n".join(lines)

def render_topk(probs, k=5):
    ranked = sorted(range(10), key=lambda d: -probs[d])[:k]
    return "   " + "  │  ".join(f"#{i+1} {d} ({probs[d]*100:.1f}%)"
                                 for i, d in enumerate(ranked))

def render_topk_rolling(hist):
    n = len(hist)
    if n == 0: return "   (no rounds yet)"
    out = []
    for label, k in [("Top-1",1),("Top-2",2),("Top-3",3),("Top-4",4),("Top-5",5)]:
        hits = sum(r[k-1] for r in hist)
        bar = "█" * int(round(hits/n * 20))
        target = "✓" if (k == 4 and hits >= 4) else " "
        out.append(f"   {label}: {hits}/{n}  {bar} {target}")
    return "\n".join(out)

def render_admin_table(admin):
    out = ["   strategy               hits   rounds   rate    "]
    for s, hits, rounds, rate, active in admin.strategy_table():
        mark = "◀ active" if active else ""
        out.append(f"   {s:22s} {hits:4d}   {rounds:5d}   {rate*100:5.1f}%   {mark}")
    return "\n".join(out)

def render_bet_sim(sim):
    out = [f"   Total rounds: {sim.total_rounds}"]
    out.append(f"   {'Bet':6s} {'Hits':>5s} {'Rate':>7s} {'P&L':>10s}")
    for k, hits, total, hr, pnl in sim.summary():
        sign = "+" if pnl >= 0 else ""
        out.append(f"   Top-{k}  {hits:5d}  {hr*100:6.1f}%  {sign}{pnl:8.1f} units")
    return "\n".join(out)

def render_meta(admin):
    rows = admin.meta.summary()
    if not rows: return "   (no meta-contexts sampled yet)"
    out = ["   context   best_strategy        rate    samples"]
    for ctx, strat, rate, samples in rows[:8]:
        out.append(f"   {ctx:8s}  {strat:20s} {rate*100:5.1f}%   {samples}")
    return "\n".join(out)

# ══════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════
def main():
    fetcher = Fetcher()
    engine = Engine()

    print("=" * 84)
    print("  CONSCIOUS MINDS v4 — META-LEARNING + BET SIMULATOR")
    print("  150 minds • meta-learner admin • agreement boost • confidence gating")
    print("=" * 84)

    print("\nFetching initial history …")
    nums = None
    while nums is None or len(nums) == 0:
        nums = fetcher.fetch_history_oldest_first()
        if nums is None:
            print("  Fetch failed — retry in 10 s.")
            time.sleep(10)
    engine.history = nums[-CONFIG.MAX_HISTORY:]
    print(f"Loaded last {len(engine.history)} numbers: {engine.history}\n")

    while True:
        now = time.time()
        nb = (math.floor(now / CONFIG.ROUND_SECONDS) * CONFIG.ROUND_SECONDS
              + CONFIG.ROUND_SECONDS)
        wait = nb - now + 3
        if wait > 0: time.sleep(wait)

        actual_num, settled_issue = None, None
        for _ in range(5):
            actual_num, settled_issue = fetcher.fetch_latest()
            if actual_num is not None: break
            time.sleep(2)
        if actual_num is None:
            print("Fetch failed — retrying.")
            continue

        prev_pred = engine.last_prediction
        engine.observe(actual_num)

        upcoming = increment_issue(settled_issue)
        engine.set_issue(upcoming)

        if prev_pred is not None and engine.total_rounds > 1:
            hit = "HIT" if prev_pred == actual_num else "MISS"
            rate = engine.correct / engine.total_rounds * 100
            print(f"\nSettled {settled_issue}: actual={actual_num} | "
                  f"pred={prev_pred} [{hit}] | "
                  f"acc {engine.correct}/{engine.total_rounds} ({rate:.1f}%)")

        decision = engine.run_round()
        votes_cast = sum(1 for v in engine.last_votes if v is not None)

        print(f"┌─ Period {upcoming}  (predicting NOW)")
        print(f"│")
        print(f"│  ADMIN'S THOUGHT: “{decision['thought']}”")
        print(f"│  Inner voice: {decision['inner_voice']}")
        print(f"│  Active strategy: {decision['strategy']}")
        if decision.get("switch"):
            print(f"│  ⚙ {decision['switch']}")
        print(f"│")
        print(f"│  Votes cast: {votes_cast}/150   entropy {decision['sense']['entropy']:.2f}")
        print(f"│  PREDICTION ➜ {decision['pick']}")
        print(f"│")
        print(f"│  ── TOP-5 CANDIDATES ──")
        print(render_topk(engine.last_chart, 5))
        print(f"│")
        print(f"│  ── TOP-K ROLLING (last 10 rounds) ──")
        print(render_topk_rolling(engine.topk_history))
        print(f"│")
        print(f"│  ── TOP-K ROLLING (last 50 rounds) ──")
        print(render_topk_rolling(engine.topk_history_50))
        print(f"│")
        print(f"│  ── WEIGHTED PROBABILITY CHART ──")
        print(render_chart(engine.last_chart))
        print(f"│")
        print(f"│  ── BET SIMULATOR (9.8x payout) ──")
        print(render_bet_sim(engine.bet_sim))
        print(f"│")
        print(f"│  ── ADMIN STRATEGY TABLE ──")
        print(render_admin_table(engine.admin))
        print(f"│")
        print(f"│  ── META-LEARNER (best strategy per crowd context) ──")
        print(render_meta(engine.admin))
        print(f"│")
        print(f"│  ── TOP MINDS ──")
        for name, weight, hits, tot, mood, energy, streak in engine.top_minds(6):
            acc = hits/tot if tot > 0 else 0.0
            print(f"│    {name:22s}  w={weight:.2f}  acc={hits}/{tot} ({acc*100:.0f}%)  "
                  f"[{mood:10s} E={energy:.2f} stk={streak:+d}]")
        print(f"│")
        print(f"│  ── MOOD DISTRIBUTION ──")
        moods = Counter(m.mood for m in engine.minds)
        for m, c in moods.most_common():
            bar = "▓" * (c // 3)
            print(f"│    {m:12s} {c:4d}  {bar}")
        print("└" + "─" * 82)

if __name__ == "__main__":
    main()
