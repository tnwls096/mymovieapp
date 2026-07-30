import time
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="박스오피스 대시보드", layout="wide")
st.title("🎬 박스오피스 대시보드")

KOBIS_KEY = st.secrets["KOBIS_KEY"]
BASE_DAILY_URL = (
    "https://www.kobis.or.kr/kobisopenapi/webservice/rest/boxoffice/"
    "searchDailyBoxOfficeList.json"
)

KST = ZoneInfo("Asia/Seoul")
today_kst = datetime.now(KST).date()
yesterday_kst = today_kst - timedelta(days=1)

# ---------------------------------------------------------------------------
# 날짜 선택 (오늘은 아직 집계 전이므로 어제까지만 선택 가능)
# ---------------------------------------------------------------------------
selected_date = st.date_input(
    "조회할 날짜를 선택하세요",
    value=yesterday_kst,
    min_value=date(2004, 1, 1),  # KOBIS 통계 시작 시점 근사치
    max_value=yesterday_kst,
    help="오늘 데이터는 아직 집계되지 않아 선택할 수 없습니다.",
)
target_dt = selected_date.strftime("%Y%m%d")
st.caption(f"조회 기준일: {selected_date.strftime('%Y-%m-%d')}")


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_daily_boxoffice(target_dt: str):
    res = requests.get(
        BASE_DAILY_URL, params={"key": KOBIS_KEY, "targetDt": target_dt}, timeout=10
    )
    res.raise_for_status()
    return res.json()


try:
    data = fetch_daily_boxoffice(target_dt)
except requests.exceptions.RequestException as e:
    st.error(f"요청이 실패했습니다: {e}")
    st.stop()

if "faultInfo" in data:
    st.error("인증키가 올바르지 않습니다. 금고(Secrets)의 KOBIS_KEY를 확인해 주세요.")
    st.stop()

box_list = data.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
if not box_list:
    st.info("그날은 아직 집계 전입니다.")
    st.stop()

df = pd.DataFrame(box_list)
for col in ["rank", "rankInten", "audiCnt", "audiAcc", "scrnCnt", "showCnt"]:
    df[col] = pd.to_numeric(df[col])

top = df.sort_values("rank").iloc[0]
c1, c2, c3 = st.columns(3)
c1.metric("1위", top["movieNm"])
c2.metric("당일 관객수", f"{top['audiCnt']:,}명")
c3.metric("누적 관객", f"{top['audiAcc']:,}명")


# ---------------------------------------------------------------------------
# 표 구성: 순위 변동 화살표 + 100만 관객 돌파 트로피
# ---------------------------------------------------------------------------
def rank_change_label(v: int) -> str:
    if v > 0:
        return f"▲{v}"
    elif v < 0:
        return f"▼{abs(v)}"
    return "-"


def style_rank_change(v: str):
    if v.startswith("▲"):
        return "color: red"
    if v.startswith("▼"):
        return "color: blue"
    return ""


table = df[
    ["rank", "movieNm", "openDt", "rankInten", "audiCnt", "audiAcc", "scrnCnt"]
].copy()
table["movieNm"] = table.apply(
    lambda r: f"{r['movieNm']} 🏆" if r["audiAcc"] >= 1_000_000 else r["movieNm"],
    axis=1,
)
table["순위변동"] = table["rankInten"].apply(rank_change_label)
table = table[["rank", "movieNm", "openDt", "순위변동", "audiCnt", "audiAcc", "scrnCnt"]]
table.columns = ["순위", "영화명", "개봉일", "순위변동", "관객수", "누적관객", "스크린수"]
table = table.sort_values("순위").reset_index(drop=True)

st.subheader("📋 박스오피스 TOP 10")
styled = table.style.map(style_rank_change, subset=["순위변동"]).format(
    {"관객수": "{:,}", "누적관객": "{:,}", "스크린수": "{:,}"}
)
st.dataframe(styled, use_container_width=True)

st.subheader("📈 관객수 상위 5편")
top5 = table.sort_values("관객수", ascending=False).head(5)
chart_labels = top5["영화명"].str.replace(" 🏆", "", regex=False)
st.bar_chart(top5.assign(영화명=chart_labels).set_index("영화명")["관객수"])


# ---------------------------------------------------------------------------
# 최근 10년 누적 관객 TOP 5 (매월 말일 스냅샷 기반 추정)
# ---------------------------------------------------------------------------
st.divider()
st.subheader("🏆 최근 10년 누적 관객 TOP 5 (추정)")
st.caption(
    "매월 말일의 일별 박스오피스 스냅샷을 모아 영화별 최대 누적 관객수를 집계한 값입니다. "
    "월말 기준 TOP10 안에 든 적이 없는 영화는 집계에서 빠질 수 있어 '추정치'이며, "
    "최초 조회 시 API를 약 120회 호출하므로 다소 시간이 걸립니다."
)


def month_end_going_back(as_of: date, months_back: int) -> date:
    """as_of 로부터 months_back개월 전 달의 마지막 날짜."""
    year, month = as_of.year, as_of.month
    for _ in range(months_back):
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    next_month_first = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return next_month_first - timedelta(days=1)


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def fetch_top_grossing_10y(as_of: date):
    snapshot_dates = [as_of] + [
        month_end_going_back(as_of, i) for i in range(1, 120)
    ]

    movies: dict[str, dict] = {}
    progress = st.progress(0.0, text="10년치 데이터를 불러오는 중입니다...")
    for idx, d in enumerate(snapshot_dates):
        try:
            res = requests.get(
                BASE_DAILY_URL,
                params={"key": KOBIS_KEY, "targetDt": d.strftime("%Y%m%d")},
                timeout=10,
            )
            if res.status_code == 200:
                day_list = res.json().get("boxOfficeResult", {}).get(
                    "dailyBoxOfficeList", []
                )
                for m in day_list:
                    cd = m["movieCd"]
                    acc = int(m["audiAcc"])
                    if cd not in movies or acc > movies[cd]["audiAcc"]:
                        movies[cd] = {"movieNm": m["movieNm"], "audiAcc": acc}
        except requests.exceptions.RequestException:
            pass
        progress.progress((idx + 1) / len(snapshot_dates))
        time.sleep(0.05)
    progress.empty()
    return movies


movies = fetch_top_grossing_10y(selected_date)
if not movies:
    st.warning("데이터를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요.")
else:
    top_df = (
        pd.DataFrame(movies.values())
        .sort_values("audiAcc", ascending=False)
        .head(5)
        .reset_index(drop=True)
    )
    top_df.index = top_df.index + 1
    top_df["audiAcc"] = top_df["audiAcc"].map(lambda v: f"{v:,}명")
    top_df.columns = ["영화명", "누적관객"]
    st.table(top_df)
