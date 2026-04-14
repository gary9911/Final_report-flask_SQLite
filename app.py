from flask import Flask, request, jsonify
from flask_cors import CORS
import sqlite3
import yfinance as yf
import time
import math
from datetime import datetime
import pytz
import requests

app = Flask(__name__)
CORS(app)

# --- 全域快取與設定 ---
price_cache = {}
CACHE_EXPIRE_SECONDS = 300
market_data_cache = {'data': {}, 'time': 0}
MARKET_CACHE_EXPIRE = 60
fgi_cache = {'data': None, 'time': 0}
FGI_CACHE_EXPIRE = 300

def get_stock_data(symbol, category):
    """取得股價與歷史區間資料，具備快取功能"""
    yf_symbol = f"{symbol}.TW" if category == 'TW' else symbol
    current_time = time.time()
    
    if yf_symbol in price_cache:
        if current_time - price_cache[yf_symbol]['time'] < CACHE_EXPIRE_SECONDS:
            return price_cache[yf_symbol]['data']
            
    try:
        ticker = yf.Ticker(yf_symbol)
        current_price = None
        past_1d = None
        
        try:
            info = ticker.info
            if 'regularMarketPrice' in info and 'regularMarketPreviousClose' in info:
                current_price = float(info['regularMarketPrice'])
                past_1d = float(info['regularMarketPreviousClose'])
        except:
            pass

        hist = ticker.history(period="1mo", auto_adjust=False).dropna(subset=['Close'])
        if hist.empty and current_price is None:
            return None
            
        if current_price is None or past_1d is None:
            current_price = float(hist['Close'].iloc[-1])
            past_1d = float(hist['Close'].iloc[-2]) if len(hist) > 1 else current_price

        def get_past_price(days_ago):
            if len(hist) > days_ago:
                return float(hist['Close'].iloc[-(days_ago + 1)])
            elif len(hist) > 1:
                return float(hist['Close'].iloc[0])
            else:
                return current_price

        stock_data = {
            "current_price": current_price,
            "past_1d": past_1d,
            "past_5d": get_past_price(5),
            "past_22d": get_past_price(22)
        }
        
        price_cache[yf_symbol] = {'data': stock_data, 'time': current_time}
        return stock_data
        
    except Exception as e:
        print(f"❌ 取得 {yf_symbol} 報價失敗: {e}")
        return None

def init_db():
    conn = sqlite3.connect('holdings.db')
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS holdings(id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT NOT NULL, symbol TEXT NOT NULL, type TEXT NOT NULL, date TEXT NOT NULL, shares REAL NOT NULL, cost REAL NOT NULL, price REAL NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS portfolio(symbol TEXT PRIMARY KEY, category TEXT NOT NULL, total_shares REAL NOT NULL, total_cost REAL NOT NULL, avg_price REAL NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS cash(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, amount REAL NOT NULL)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS asset_history(date TEXT PRIMARY KEY, total_val REAL NOT NULL, stock_val REAL NOT NULL, cash_val REAL NOT NULL)''')
    
    cursor.execute("SELECT COUNT(*) FROM cash")
    if cursor.fetchone()[0] == 0:
        cursor.execute("INSERT INTO cash (name, amount) VALUES ('活期存款', 500000)")
    conn.commit()
    conn.close()

def recalculate_portfolio():
    conn = sqlite3.connect('holdings.db')
    cursor = conn.cursor()
    cursor.execute("SELECT symbol, category, type, shares, cost FROM holdings")
    rows = cursor.fetchall()
    
    summary = {}
    for row in rows:
        symbol, category, h_type, shares, cost = row
        if symbol not in summary:
            summary[symbol] = {'category': category, 'shares': 0, 'cost': 0}
        
        if h_type == 'buy':
            summary[symbol]['shares'] += shares
            summary[symbol]['cost'] += cost
        elif h_type == 'sell':
            if summary[symbol]['shares'] > 0:
                ratio = shares / summary[symbol]['shares']
                summary[symbol]['cost'] -= (summary[symbol]['cost'] * ratio)
            summary[symbol]['shares'] -= shares
        elif h_type == 'other':
            summary[symbol]['shares'] += shares
            summary[symbol]['cost'] += cost

    cursor.execute("DELETE FROM portfolio")
    for symbol, data in summary.items():
        if data['shares'] > 0:
            avg_price = data['cost'] / data['shares']
            cursor.execute("INSERT INTO portfolio (symbol, category, total_shares, total_cost, avg_price) VALUES (?, ?, ?, ?, ?)", 
                           (symbol, data['category'], data['shares'], data['cost'], avg_price))
    conn.commit()
    conn.close()

@app.route('/market_data', methods=['GET'])
def get_market_data():
    global market_data_cache
    current_time = time.time()
    
    if current_time - market_data_cache['time'] < MARKET_CACHE_EXPIRE and market_data_cache['data']:
        return jsonify(market_data_cache['data']), 200

    symbols_map = {
        "twii": "^TWII", "txf": "EWT", "spx": "^GSPC", "twd": "TWD=X", "vix": "^VIX", "brent": "BZ=F"
    }
    
    result = {}
    taipei_tz = pytz.timezone('Asia/Taipei')
    now_taipei = datetime.now(taipei_tz)

    for key, symbol in symbols_map.items():
        try:
            ticker = yf.Ticker(symbol)
            fast = ticker.fast_info
            hist = ticker.history(period="7d")
            
            price = None
            prev_close = None
            market_ts = None
            
            try:
                price = float(fast.last_price)
                prev_close = float(fast.previous_close)
                market_ts = fast.timestamp
            except:
                pass
            
            if price is None or math.isnan(price):
                if not hist.empty:
                    price = float(hist['Close'].iloc[-1])
                    prev_close = float(hist['Close'].iloc[-2]) if len(hist) > 1 else price
                    market_ts = hist.index[-1].timestamp()
                else:
                    raise ValueError("No data")
            
            if market_ts is None: market_ts = time.time()

            dt_object = datetime.fromtimestamp(market_ts, pytz.utc).astimezone(taipei_tz)
            is_history = (now_taipei.timestamp() - market_ts) > 1800
            precise_time = dt_object.strftime('%m/%d %H:%M:%S')

            diff = price - prev_close if prev_close else 0
            ratio = (diff / prev_close) * 100 if prev_close else 0

            result[key] = {
                "price": price, "diff": diff, "ratio": ratio,
                "status": "已收盤" if is_history else "開盤中",
                "is_history": is_history, "time": precise_time
            }
        except Exception as e:
            print(f"Error fetching {symbol}: {e}")
            result[key] = {"price": None, "diff": 0, "ratio": 0, "status": "--", "time": "--"}
            
    market_data_cache['data'] = result
    market_data_cache['time'] = current_time
    return jsonify(result), 200

@app.route('/fear_greed', methods=['GET'])
def get_fear_greed():
    global fgi_cache
    current_time = time.time()
    
    if current_time - fgi_cache['time'] < FGI_CACHE_EXPIRE and fgi_cache['data']:
        return jsonify(fgi_cache['data']), 200

    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    
    # 加入更完整的 Header，模擬真實網頁請求，降低被 Cloudflare 擋下的機率
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7",
        "Referer": "https://www.cnn.com/",
        "Origin": "https://www.cnn.com",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "cross-site",
        "Connection": "keep-alive"
    }
    
    try:
        # 稍微延長 timeout 到 10 秒
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        score = data['fear_and_greed']['score']
        rating = data['fear_and_greed']['rating']
        
        rating_map = {
            'extreme greed': '極度貪婪', 'greed': '貪婪', 'neutral': '中立', 
            'fear': '恐懼', 'extreme fear': '極度恐懼'
        }
        
        taipei_tz = pytz.timezone('Asia/Taipei')
        now_str = datetime.now(taipei_tz).strftime('%m/%d %H:%M:%S')
        
        result = {
            "score": round(score, 0), "rating": rating_map.get(rating.lower(), rating),
            "time": now_str, "is_history": False
        }
        fgi_cache['data'] = result
        fgi_cache['time'] = current_time
        return jsonify(result), 200
        
    except requests.exceptions.RequestException as e:
        print(f"❌ 網路請求失敗 (CNN 阻擋或超時): {e}")
        # 改回傳 200 狀態碼，讓前端正常顯示「連線被拒」，不噴 500 錯誤
        return jsonify({"score": "--", "rating": "連線被拒", "time": "--", "is_history": False}), 200
    except KeyError as e:
        print(f"❌ JSON 解析失敗 (CNN 結構可能改變): {e}")
        return jsonify({"score": "--", "rating": "格式錯誤", "time": "--", "is_history": False}), 200
    except Exception as e:
        print(f"❌ 未知錯誤: {e}")
        return jsonify({"score": "--", "rating": "系統錯誤", "time": "--", "is_history": False}), 200

@app.route('/portfolio', methods=['GET'])
def get_portfolio_summary():
    try:
        recalculate_portfolio()
        conn = sqlite3.connect('holdings.db')
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM portfolio")
        port_rows = cursor.fetchall()
        cursor.execute("SELECT * FROM cash")
        cash_rows = cursor.fetchall()
        conn.close()

        exrate_data = get_stock_data("TWD=X", "US") 
        current_exrate = 32.0 
        if exrate_data and exrate_data.get("current_price"):
            current_exrate = float(exrate_data["current_price"])

        portfolio_result = []
        for r in port_rows:
            symbol, category, total_shares, total_cost, avg_price = r[0], r[1], r[2], r[3], r[4]
            stock_info = get_stock_data(symbol, category)
            fallback_price = avg_price / current_exrate if category == 'US' else avg_price
            
            cp = past1 = past5 = past22 = fallback_price
            if stock_info:
                cp = stock_info["current_price"] or fallback_price
                past1 = stock_info["past_1d"] or fallback_price
                past5 = stock_info["past_5d"] or fallback_price
                past22 = stock_info["past_22d"] or fallback_price

            portfolio_result.append({
                "symbol": symbol, "category": category, "total_shares": total_shares, 
                "total_cost": total_cost, "avg_price": avg_price, "current_price": cp, 
                "past_1d": past1, "past_5d": past5, "past_22d": past22
            })
            
        return jsonify({"portfolio": portfolio_result, "cash": [{"id": c[0], "name": c[1], "amount": c[2]} for c in cash_rows], "exchange_rate": current_exrate}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/history', methods=['GET', 'POST'])
def handle_history():
    conn = sqlite3.connect('holdings.db')
    cursor = conn.cursor()
    if request.method == 'GET':
        cursor.execute("SELECT date, total_val, stock_val, cash_val FROM asset_history ORDER BY date ASC")
        rows = cursor.fetchall()
        conn.close()
        return jsonify([{"date": r[0], "total": r[1], "stock": r[2], "cash": r[3]} for r in rows]), 200
    else:
        data = request.get_json()
        cursor.execute("INSERT OR REPLACE INTO asset_history VALUES (?, ?, ?, ?)", (data.get('date'), data.get('total'), data.get('stock'), data.get('cash')))
        conn.commit()
        conn.close()
        return jsonify({"message": "success"}), 200

@app.route('/holdings', methods=['GET', 'POST'])
def handle_holdings():
    conn = sqlite3.connect('holdings.db')
    cursor = conn.cursor()
    if request.method == 'GET':
        cursor.execute("SELECT * FROM holdings")
        rows = cursor.fetchall()
        conn.close()
        return jsonify({"holdings": [{"id":r[0], "category":r[1], "symbol":r[2], "type":r[3], "date":r[4], "shares":r[5], "cost":r[6], "price":r[7]} for r in rows]}), 200
    else:
        data = request.get_json()
        cursor.execute("INSERT INTO holdings(category, symbol, type, date, shares, cost, price) VALUES(?, ?, ?, ?, ?, ?, ?)", 
                       (data.get('category'), data.get('symbol'), data.get('type'), data.get('date'), data.get('shares'), data.get('cost'), data.get('price')))
        conn.commit()
        conn.close()
        recalculate_portfolio()
        return jsonify({"message": "success"}), 201

@app.route('/holdings/<int:pid>', methods=['PUT', 'DELETE'])
def update_delete_holdings(pid):
    conn = sqlite3.connect("holdings.db")
    cursor = conn.cursor()
    if request.method == 'PUT':
        data = request.get_json()
        cursor.execute("UPDATE holdings SET category=?, symbol=?, type=?, date=?, shares=?, cost=?, price=? WHERE id=?", 
                       (data.get('category'), data.get('symbol'), data.get('type'), data.get('date'), data.get('shares'), data.get('cost'), data.get('price'), pid))
    else:
        cursor.execute("DELETE FROM holdings WHERE id = ?", (pid,))
    conn.commit()
    conn.close()
    recalculate_portfolio()
    return jsonify({"message": "success"}), 200

@app.route('/cash/<int:cid>', methods=['PUT'])
def update_cash(cid):
    data = request.get_json()
    conn = sqlite3.connect("holdings.db")
    cursor = conn.cursor()
    cursor.execute("UPDATE cash SET amount = ? WHERE id = ?", (data.get('amount'), cid))
    conn.commit()
    conn.close()
    return jsonify({"message": "success"}), 200

if __name__ == '__main__':
    init_db() 
    app.run(debug=True)