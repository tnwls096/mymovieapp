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


def nearest_friday_on_or_after(d: date) -> date:
    """d(개봉일)를 포함해 그 이후 첫 금요일을 반환."""
    days_ahead = (4 - d.weekday()) % 7  # 월=0 ... 금=4
    return d + timedelta(days=days_ahead)


def weekend_audience_sum(movie_cd: str, friday: date, cutoff: date):
    """friday, friday+1(토), friday+2(일) 3일치 관객수 합계.
    cutoff(어제)를 넘는 날짜나 그날 TOP10에 없는 경우는 missing에 기록."""
    total = 0
    missing = []
    for offset in range(3):
        d = friday + timedelta(days=offset)
        if d > cutoff:
            missing.append(d)
            continue
        try:
            day_data = fetch_daily_boxoffice(d.strftime("%Y%m%d"))
            day_list = day_data.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        except requests.exceptions.RequestException:
            missing.append(d)
            continue
        match = next((m for m in day_list if m["movieCd"] == movie_cd), None)
        if match:
            total += int(match["audiCnt"])
        else:
            missing.append(d)
    return total, missing


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
# 스크린·상영 점유율 (당일 TOP10 기준 독과점 지수)
# ---------------------------------------------------------------------------
st.divider()
st.subheader("🎯 스크린·상영 점유율")
st.caption(
    "KOBIS API는 그날 전국 전체 스크린 수는 제공하지 않아, "
    "그날 TOP10에 오른 영화들의 스크린수·상영횟수 합계를 기준으로 점유율을 계산합니다."
)

occ = df[["movieNm", "scrnCnt", "showCnt"]].copy()
total_scrn = occ["scrnCnt"].sum()
total_show = occ["showCnt"].sum()
occ["스크린 점유율"] = (occ["scrnCnt"] / total_scrn * 100).round(1)
occ["상영 점유율"] = (occ["showCnt"] / total_show * 100).round(1)
occ = occ.sort_values("스크린 점유율", ascending=False).reset_index(drop=True)

top_occ = occ.iloc[0]
o1, o2 = st.columns(2)
o1.metric(
    f"{top_occ['movieNm']} 스크린 점유율",
    f"{top_occ['스크린 점유율']}%",
    help="TOP10 스크린수 합계 대비 비율",
)
o2.metric(
    f"{top_occ['movieNm']} 상영 점유율",
    f"{top_occ['상영 점유율']}%",
    help="TOP10 상영횟수 합계 대비 비율",
)
st.bar_chart(occ.set_index("movieNm")[["스크린 점유율", "상영 점유율"]])


# ---------------------------------------------------------------------------
# 드롭오프율: 1주 차 주말 vs 2주 차 주말
# ---------------------------------------------------------------------------
st.divider()
st.subheader("📉 드롭오프율 (입소문형 vs 초반 화력형)")
st.caption(
    "개봉 1주 차 주말(금·토·일) 대비 2주 차 주말 관객수 감소율입니다. "
    "해당 요일에 TOP10 밖으로 밀려난 영화는 정확히 계산되지 않을 수 있습니다."
)

movie_choice = st.selectbox(
    "분석할 영화를 선택하세요",
    options=df.sort_values("rank")["movieCd"],
    format_func=lambda cd: df.loc[df["movieCd"] == cd, "movieNm"].values[0],
    key="dropoff_movie",
)

if st.button("드롭오프율 계산하기"):
    row = df.loc[df["movieCd"] == movie_choice].iloc[0]
    open_dt = datetime.strptime(row["openDt"], "%Y-%m-%d").date()
    week1_fri = nearest_friday_on_or_after(open_dt)
    week2_fri = week1_fri + timedelta(days=7)

    week1_total, week1_missing = weekend_audience_sum(movie_choice, week1_fri, yesterday_kst)
    week2_total, week2_missing = weekend_audience_sum(movie_choice, week2_fri, yesterday_kst)

    if week2_fri + timedelta(days=2) > yesterday_kst:
        st.info("2주 차 주말이 아직 지나지 않아 데이터가 없습니다.")
    elif week1_missing or week2_missing:
        st.warning(
            "일부 날짜에 이 영화가 TOP10 밖이라 정확한 값을 구하기 어렵습니다. 참고용으로만 봐주세요."
        )

    if week1_total > 0:
        drop_rate = (week1_total - week2_total) / week1_total * 100
        d1, d2, d3 = st.columns(3)
        d1.metric("1주 차 주말 관객수", f"{week1_total:,}명")
        d2.metric("2주 차 주말 관객수", f"{week2_total:,}명")
        d3.metric("드롭오프율", f"{drop_rate:.1f}%")
        if drop_rate >= 50:
            st.write("👉 감소폭이 커서 **초반 화력형**에 가깝습니다.")
        elif drop_rate <= 20:
            st.write("👉 감소폭이 작아 **입소문 확산형**에 가깝습니다.")
        else:
            st.write("👉 평균적인 흥행 패턴입니다.")
    else:
        st.error("1주 차 주말 데이터를 구할 수 없습니다 (TOP10 밖이었거나 아직 개봉 전).")


# ---------------------------------------------------------------------------
# 개봉 요일별(수요일 vs 목/금요일) 첫 주말 흥행 비교
# ---------------------------------------------------------------------------
st.divider()
st.subheader("📅 개봉 요일별 첫 주말 흥행 비교")
st.caption(
    "최근 16주간 금요일 박스오피스 차트에 새로 등장한 영화를 모아, "
    "수요일 개봉작과 목/금요일 개봉작의 개봉 첫 주말(금·토·일) 평균 관객수를 비교합니다. "
    "표본이 많지 않을 수 있어 참고용 통계입니다."
)


@st.cache_data(ttl=60 * 60 * 24, show_spinner=False)
def analyze_opening_weekday(as_of: date, weeks_back: int = 16):
    last_friday = as_of - timedelta(days=(as_of.weekday() - 4) % 7)
    fridays = [last_friday - timedelta(weeks=i) for i in range(weeks_back)]

    seen = set()
    wed_group = []
    thufri_group = []

    progress = st.progress(0.0, text="개봉 요일별 데이터를 모으는 중입니다...")
    for idx, fri in enumerate(fridays):
        try:
            day_data = fetch_daily_boxoffice(fri.strftime("%Y%m%d"))
            day_list = day_data.get("boxOfficeResult", {}).get("dailyBoxOfficeList", [])
        except requests.exceptions.RequestException:
            day_list = []

        for m in day_list:
            cd = m["movieCd"]
            if cd in seen:
                continue
            try:
                open_dt = datetime.strptime(m["openDt"], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue

            if open_dt == fri - timedelta(days=2):
                group = "수요일 개봉"
            elif open_dt in (fri - timedelta(days=1), fri):
                group = "목/금요일 개봉"
            else:
                continue

            seen.add(cd)
            total, missing = weekend_audience_sum(cd, fri, as_of)
            if not missing and total > 0:
                (wed_group if group == "수요일 개봉" else thufri_group).append(total)

        progress.progress((idx + 1) / len(fridays))
        time.sleep(0.05)
    progress.empty()
    return wed_group, thufri_group


if st.button("개봉 요일 비교 계산하기 (최초 실행 시 시간이 걸립니다)"):
    wed_group, thufri_group = analyze_opening_weekday(yesterday_kst)
    if not wed_group and not thufri_group:
        st.warning("최근 16주 내에서 표본을 충분히 모으지 못했습니다.")
    else:
        summary = pd.DataFrame(
            {
                "구분": ["수요일 개봉", "목/금요일 개봉"],
                "평균 첫 주말 관객수": [
                    sum(wed_group) / len(wed_group) if wed_group else 0,
                    sum(thufri_group) / len(thufri_group) if thufri_group else 0,
                ],
                "표본 수": [len(wed_group), len(thufri_group)],
            }
        )
        st.bar_chart(summary.set_index("구분")["평균 첫 주말 관객수"])
        st.dataframe(
            summary.style.format({"평균 첫 주말 관객수": "{:,.0f}"}),
            use_container_width=True,
        )


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
