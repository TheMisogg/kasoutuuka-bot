import json, threading, time
from typing import List, Dict, Any, Optional
from websocket import WebSocketApp

WS_PUBLIC = "wss://stream.bybit.com/v5/public/linear"

class BybitWS:
    """
    WebSocket subscriptions (public):
      - orderbook.50.<symbol>
      - publicTrade.<symbol>
      - liquidation.<symbol>   （環境によって購読不可の場合あり）
    """
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.ws: Optional[WebSocketApp] = None
        self.thread: Optional[threading.Thread] = None
        self.connected = False

        # shared states
        self.orderbook: Dict[str, List[List[float]]] = {"bids": [], "asks": []}  # list of [price, size] sorted
        self.trades: List[Dict[str, Any]] = []     # recent trades
        self.liquidations: List[Dict[str, Any]] = []

        self._lock = threading.Lock()

    def _on_open(self, ws):
        self.connected = True
        # subscribe topics
        sub = {
            "op": "subscribe",
            "args": [
                f"orderbook.50.{self.symbol}",
                f"publicTrade.{self.symbol}",
                f"liquidation.{self.symbol}"
            ]
        }
        ws.send(json.dumps(sub))

    def _on_close(self, ws, code, msg):
        self.connected = False

    def _on_error(self, ws, err):
        self.connected = False

    def _on_message(self, ws, msg):
        try:
            obj = json.loads(msg)
        except Exception:
            return
        topic = obj.get("topic")
        if not topic:
            return
        if topic.startswith("orderbook"):
            self._handle_orderbook(obj)
        elif topic.startswith("publicTrade"):
            self._handle_trade(obj)
        elif topic.startswith("liquidation"):
            self._handle_liq(obj)

    def _handle_orderbook(self, obj):
        # 期待形式：{'type':'snapshot'|'delta', 'data':{'b':[ [price, size],... ], 'a':[...]}}
        data = obj.get("data", {})
        typ = obj.get("type")
        with self._lock:
            if typ == "snapshot":
                bids = data.get("b", [])
                asks = data.get("a", [])
                # price/size as strings -> floats
                self.orderbook["bids"] = [[float(p), float(s)] for p, s in bids]
                self.orderbook["asks"] = [[float(p), float(s)] for p, s in asks]
            elif typ == "delta":
                # apply deltas (簡易：全量置換に近い更新)
                bids = data.get("b", [])
                asks = data.get("a", [])
                if bids:
                    self._merge_side(self.orderbook["bids"], bids, is_bid=True)
                if asks:
                    self._merge_side(self.orderbook["asks"], asks, is_bid=False)

    def _merge_side(self, side_list, deltas, is_bid):
        # deltas: [[price, size]] size=0 は削除
        m = {p: s for p, s in side_list}
        for p, s in deltas:
            p = float(p); s = float(s)
            if s == 0:
                if p in m:
                    del m[p]
            else:
                m[p] = s
        # rebuild sorted
        items = sorted(m.items(), key=lambda x: x[0], reverse=is_bid)
        side_list[:] = [[k, v] for k, v in items[:50]]

    def _handle_trade(self, obj):
        dlist = obj.get("data", [])
        with self._lock:
            for d in dlist:
                # {'T': ts, 'S': side, 'v': qty, 'p': price, ...}
                self.trades.append({
                    "ts": int(d.get("T", 0)),
                    "side": d.get("S", ""),
                    "qty": float(d.get("v", 0) or 0.0),
                    "price": float(d.get("p", 0) or 0.0),
                })
            self.trades = self.trades[-2000:]

    def _handle_liq(self, obj):
        dlist = obj.get("data", [])
        with self._lock:
            for d in dlist:
                # 実際のキー名は環境に依存する可能性あり。安全に吸収。
                self.liquidations.append({
                    "ts": int(d.get("T", d.get("ts", 0)) or 0),
                    "side": d.get("S", d.get("side", "")),
                    "qty": float(d.get("v", d.get("qty", 0)) or 0.0),
                    "price": float(d.get("p", d.get("price", 0)) or 0.0),
                })
            self.liquidations = self.liquidations[-2000:]

    def start(self):
        def run():
            while True:
                try:
                    self.ws = WebSocketApp(
                        WS_PUBLIC,
                        on_open=self._on_open,
                        on_message=self._on_message,
                        on_error=self._on_error,
                        on_close=self._on_close
                    )
                    self.ws.run_forever(ping_interval=20, ping_timeout=10)
                except Exception:
                    pass
                time.sleep(3)  # reconnect backoff
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    # snapshots
    def snapshot_orderbook(self):
        with self._lock:
            return {
                "bids": [x[:] for x in self.orderbook["bids"]],
                "asks": [x[:] for x in self.orderbook["asks"]],
            }

    def snapshot_trades(self):
        with self._lock:
            return self.trades[:]  # shallow copy

    def snapshot_liq(self):
        with self._lock:
            return self.liquidations[:]
