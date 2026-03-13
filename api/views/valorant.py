from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings

from rest_framework.response import Response
from rest_framework.views import APIView

from ..auth import InternalAPIKeyPermission
from ..models import AccountLink, ValorantMap
from ..integrations.riot import account_by_riot_id
from ..integrations.val_match import match_by_id, matchlist_by_puuid


def _map_name(map_id: str) -> str:
    raw = (map_id or "").strip()
    if not raw:
        return raw

    tail = raw.strip("/").split("/")[-1] or raw
    exact = (
        ValorantMap.objects.filter(asset_path=raw)
        .values_list("display_name", flat=True)
        .first()
    )
    if exact:
        return exact

    fallback = (
        ValorantMap.objects.filter(asset_name=tail)
        .values_list("display_name", flat=True)
        .first()
    )
    if fallback:
        return fallback

    return tail


def _safe_int(v, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _format_jst_datetime_from_millis(v) -> str:
    ms = _safe_int(v, 0)
    if ms <= 0:
        return "不明"
    dt_utc = datetime.fromtimestamp(ms / 1000, tz=dt_timezone.utc)
    dt_jst = dt_utc.astimezone(dt_timezone(timedelta(hours=9)))
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekdays[dt_jst.weekday()]
    return f"{dt_jst.year}年{dt_jst.month}月{dt_jst.day}日({wd}) {dt_jst:%H:%M}ごろ"


def _compute_match_highlights(match_data: dict, my_puuid: str) -> dict:
    players = match_data.get("players", []) or []
    teams = match_data.get("teams", []) or []
    rounds = match_data.get("roundResults", []) or []
    info = match_data.get("matchInfo", {}) or {}

    me = next((p for p in players if p.get("puuid") == my_puuid), None)
    if not me:
        raise ValueError("player_not_found_in_match")

    my_team_id = me.get("teamId")
    my_team = next((t for t in teams if t.get("teamId") == my_team_id), None)
    enemy_team = next((t for t in teams if t.get("teamId") != my_team_id), None)

    my_stats = me.get("stats", {}) or {}
    kills = _safe_int(my_stats.get("kills"))
    deaths = _safe_int(my_stats.get("deaths"))
    assists = _safe_int(my_stats.get("assists"))
    score = _safe_int(my_stats.get("score"))
    rounds_played = max(_safe_int(my_stats.get("roundsPlayed"), 1), 1)
    hs_total = 0
    shot_total = 0
    max_damage_round = {"roundNum": None, "damage": 0}
    multi_kill_rounds = []
    clutch_wins = []
    ace_rounds = []
    first_blood_rounds = []
    first_death_rounds = []
    survived_rounds = 0
    kast_rounds = 0
    traded_death_rounds = set()

    team_side_by_round = []
    my_round_wins = []
    my_team_score_prog = []
    enemy_score_prog = []
    my_score = 0
    enemy_score = 0
    round_timeline = []
    clutch_attempts = []

    my_team_members = {p.get("puuid") for p in players if p.get("teamId") == my_team_id}
    enemy_members = {p.get("puuid") for p in players if p.get("teamId") != my_team_id}

    for rr in rounds:
        round_num = _safe_int(rr.get("roundNum"), -1)
        winning_team = rr.get("winningTeam")
        my_win = winning_team == my_team_id
        my_round_wins.append(my_win)
        if my_win:
            my_score += 1
        else:
            enemy_score += 1
        my_team_score_prog.append(my_score)
        enemy_score_prog.append(enemy_score)

        team_stats = rr.get("playerStats", []) or []
        by_puuid = {ps.get("puuid"): (ps or {}) for ps in team_stats}
        my_ps = by_puuid.get(my_puuid, {})

        round_kills = my_ps.get("kills", []) or []
        kcount = len(round_kills)
        if kcount >= 2:
            multi_kill_rounds.append({"roundNum": round_num, "kills": kcount})
        if kcount >= 5:
            ace_rounds.append(round_num)

        round_damage = 0
        for d in (my_ps.get("damage", []) or []):
            round_damage += _safe_int(d.get("damage"))
            hs_total += _safe_int(d.get("headshots"))
            shot_total += _safe_int(d.get("headshots")) + _safe_int(d.get("bodyshots")) + _safe_int(d.get("legshots"))
        if round_damage > max_damage_round["damage"]:
            max_damage_round = {"roundNum": round_num, "damage": round_damage}

        all_kills = []
        for ps in team_stats:
            for k in (ps.get("kills", []) or []):
                if k:
                    all_kills.append(k)
        all_kills.sort(key=lambda x: _safe_int(x.get("timeSinceRoundStartMillis")))
        if all_kills:
            first = all_kills[0]
            if first.get("killer") == my_puuid:
                first_blood_rounds.append(round_num)
            if first.get("victim") == my_puuid:
                first_death_rounds.append(round_num)

        kill_events = []
        death_time = None
        for k in all_kills:
            t = _safe_int(k.get("timeSinceRoundStartMillis"))
            killer = k.get("killer")
            victim = k.get("victim")
            if killer in my_team_members and victim in enemy_members:
                kill_events.append(("team_kill", t, killer, victim))
            elif killer in enemy_members and victim in my_team_members:
                kill_events.append(("enemy_kill", t, killer, victim))

        team_alive = len(my_team_members)
        enemy_alive = len(enemy_members)
        my_alive = True
        clutch_attempt_vs = 0
        my_death_time = None
        for kind, t, _killer, victim in kill_events:
            if kind == "team_kill":
                enemy_alive = max(enemy_alive - 1, 0)
            else:
                team_alive = max(team_alive - 1, 0)
                if victim == my_puuid and death_time is None:
                    death_time = t
                    my_death_time = t
                    my_alive = False

            if my_alive and team_alive == 1 and enemy_alive >= 2:
                clutch_attempt_vs = max(clutch_attempt_vs, enemy_alive)

            if death_time is not None and kind == "team_kill" and t <= death_time + 8000:
                traded_death_rounds.add(round_num)
                death_time = None

        my_death = any((k.get("victim") == my_puuid) for k in all_kills)
        survived_this_round = not my_death
        if survived_this_round:
            survived_rounds += 1

        round_assist = any(my_puuid in ((k.get("assistants") or [])) for k in all_kills)
        traded = round_num in traded_death_rounds
        if (kcount > 0) or round_assist or survived_this_round or traded:
            kast_rounds += 1

        won = winning_team == my_team_id

        role = rr.get("winningTeamRole")
        my_side = "-"
        if role == "Attacker":
            my_side = "ATK" if my_win else "DEF"
            team_side_by_round.append(my_side)
        elif role == "Defender":
            my_side = "DEF" if my_win else "ATK"
            team_side_by_round.append(my_side)
        else:
            team_side_by_round.append("-")

        clutch_won = bool(won and clutch_attempt_vs >= 2 and not my_death)
        if clutch_attempt_vs >= 2:
            clutch_attempt = {
                "roundNum": round_num,
                "vs": clutch_attempt_vs,
                "won": clutch_won,
                "kills": kcount,
            }
            clutch_attempts.append(clutch_attempt)
            if clutch_won:
                clutch_wins.append(clutch_attempt)

        round_timeline.append(
            {
                "roundNum": round_num,
                "side": my_side,
                "won": my_win,
                "scoreAfterRound": f"{my_score}-{enemy_score}",
                "kills": kcount,
                "death": my_death,
                "assisted": round_assist,
                "damage": round_damage,
                "firstBlood": round_num in first_blood_rounds,
                "firstDeath": round_num in first_death_rounds,
                "multiKill": kcount if kcount >= 2 else 0,
                "clutchAttemptVs": clutch_attempt_vs if clutch_attempt_vs >= 2 else 0,
                "clutchWon": clutch_won,
                "survived": survived_this_round,
                "tradedDeath": traded,
                "deathTimeMs": my_death_time,
            }
        )

    max_deficit = 0
    max_lead = 0
    lead_seen = False
    come_from_behind = False
    max_deficit_round = None
    for idx, (ms, es) in enumerate(zip(my_team_score_prog, enemy_score_prog)):
        diff = ms - es
        if diff > 0:
            lead_seen = True
        if diff > max_lead:
            max_lead = diff
        max_deficit = min(max_deficit, diff)
        if diff == max_deficit:
            max_deficit_round = idx
        if lead_seen and diff < 0:
            pass
        if max_deficit <= -5 and diff > 0:
            come_from_behind = True

    total_rounds = len(rounds) if rounds else rounds_played
    half = total_rounds // 2
    first_half_wins = sum(1 for i in range(min(half, len(my_round_wins))) if my_round_wins[i])
    first_half_losses = max(half - first_half_wins, 0)
    second_half_wins = sum(1 for i in range(half, len(my_round_wins)) if my_round_wins[i])
    second_half_losses = max(len(my_round_wins) - half - second_half_wins, 0)

    my_rounds_won = _safe_int((my_team or {}).get("roundsWon"))
    enemy_rounds_won = _safe_int((enemy_team or {}).get("roundsWon"))
    ot_match = (my_rounds_won + enemy_rounds_won) >= 25

    defender_collapsed = False
    attacker_dominant = False

    # ★BUGFIX: ATK/DEF勝率は「sideだけ」じゃなく「そのラウンドの勝敗」と紐付ける
    atk_rounds = atk_wins = 0
    def_rounds = def_wins = 0
    for i, side in enumerate(team_side_by_round):
        w = my_round_wins[i] if i < len(my_round_wins) else False
        if side == "ATK":
            atk_rounds += 1
            if w:
                atk_wins += 1
        elif side == "DEF":
            def_rounds += 1
            if w:
                def_wins += 1

    if def_rounds >= 4 and (def_wins / def_rounds) <= 0.3:
        defender_collapsed = True
    if atk_rounds >= 4 and (atk_wins / atk_rounds) >= 0.7:
        attacker_dominant = True

    max_win_streak = 0
    max_lose_streak = 0
    cur_win = 0
    cur_lose = 0
    for w in my_round_wins:
        if w:
            cur_win += 1
            cur_lose = 0
        else:
            cur_lose += 1
            cur_win = 0
        if cur_win > max_win_streak:
            max_win_streak = cur_win
        if cur_lose > max_lose_streak:
            max_lose_streak = cur_lose

    impactful_round = None
    best_impact = -1
    for r in round_timeline:
        impact = r["damage"] + (r["kills"] * 120)
        if r["clutchWon"]:
            impact += 450
        if r["multiKill"] >= 4:
            impact += 250
        if r["firstBlood"]:
            impact += 60
        if impact > best_impact:
            best_impact = impact
            impactful_round = r

    story_lines = []
    opening_rounds = min(6, len(my_round_wins))
    if opening_rounds >= 4:
        opening_wins = sum(1 for i in range(opening_rounds) if my_round_wins[i])
        if opening_wins <= 2:
            story_lines.append(f"立ち上がりは{opening_wins}-{opening_rounds - opening_wins}で重い展開")
        elif opening_wins >= 4:
            story_lines.append(f"序盤{opening_wins}-{opening_rounds - opening_wins}で主導権を握った")

    if half > 0:
        story_lines.append(
            f"前半 {first_half_wins}-{first_half_losses}、後半 {second_half_wins}-{second_half_losses}"
        )

    if come_from_behind:
        story_lines.append("中盤までのビハインドを終盤でひっくり返した")
    elif max_deficit <= -4 and not bool((my_team or {}).get("won", False)):
        story_lines.append("一度離された点差を詰め切れずに終了")

    if max_win_streak >= 4:
        story_lines.append(f"{max_win_streak}連取の流れを作れた")
    if max_lose_streak >= 4:
        story_lines.append(f"{max_lose_streak}連敗の時間帯が重かった")

    if impactful_round:
        extra = []
        if impactful_round["clutchWon"]:
            extra.append(f"1v{impactful_round['clutchAttemptVs']}クラッチ")
        if impactful_round["multiKill"] >= 3:
            extra.append(f"{impactful_round['multiKill']}K")
        extra_txt = f"（{' / '.join(extra)}）" if extra else ""
        story_lines.append(
            f"ターニングポイントはR{impactful_round['roundNum']}の{impactful_round['damage']}ダメージ{extra_txt}"
        )

    hs_rate = (hs_total / shot_total * 100.0) if shot_total else 0.0
    kd = (kills / deaths) if deaths > 0 else float(kills)
    kast = kast_rounds / max(total_rounds, 1) * 100.0
    survival_rate = survived_rounds / max(total_rounds, 1) * 100.0

    clutch_breakdown = {}
    for c in clutch_wins:
        key = f"1v{c['vs']}"
        clutch_breakdown[key] = clutch_breakdown.get(key, 0) + 1

    return {
        "map": _map_name(info.get("mapId", "")),
        "queueId": info.get("queueId", ""),
        "won": bool((my_team or {}).get("won", False)),
        "scoreline": f"{my_rounds_won}-{enemy_rounds_won}",
        "kda": {"k": kills, "d": deaths, "a": assists},
        "kd": round(kd, 2),
        "hs_rate": round(hs_rate, 1),
        "survival_rate": round(survival_rate, 1),
        "kast_like": round(kast, 1),
        "acs": round(score / max(rounds_played, 1), 1),
        "ace_count": len(ace_rounds),
        "ace_rounds": ace_rounds,
        "triple_plus_kills": sum(1 for x in multi_kill_rounds if x["kills"] >= 3),
        "clutch_count": len(clutch_wins),
        "clutch_attempt_count": len(clutch_attempts),
        "clutch_breakdown": clutch_breakdown,
        "clutches": clutch_wins,
        "first_blood_count": len(first_blood_rounds),
        "first_death_count": len(first_death_rounds),
        "multi_kills": {
            "2k": sum(1 for x in multi_kill_rounds if x["kills"] == 2),
            "3k": sum(1 for x in multi_kill_rounds if x["kills"] == 3),
            "4k": sum(1 for x in multi_kill_rounds if x["kills"] == 4),
            "5k": sum(1 for x in multi_kill_rounds if x["kills"] >= 5),
            "total_multi_rounds": len(multi_kill_rounds),
        },
        "max_damage_round": max_damage_round,
        "narratives": {
            "big_deficit_comeback_win": come_from_behind and bool((my_team or {}).get("won", False)),
            "lost_first_half_but_recovered": first_half_losses > first_half_wins and second_half_wins >= second_half_losses,
            "overtime_battle": ot_match,
            "defense_collapsed": defender_collapsed,
            "attack_worked": attacker_dominant,
            "max_deficit": abs(max_deficit),
            "max_lead": max_lead,
            "win_streak_max": max_win_streak,
            "lose_streak_max": max_lose_streak,
            "max_deficit_round": max_deficit_round,
        },
        "story_lines": story_lines,
        "round_timeline": round_timeline,
    }


def _build_discord_match_message(riot_id: str, analysis: dict) -> str:
    k = analysis["kda"]["k"]
    d = analysis["kda"]["d"]
    a = analysis["kda"]["a"]
    result = "WIN" if analysis["won"] else "LOSE"
    lines = [
        f"【{result}】{riot_id}",
        f"試合開始: {analysis.get('game_start_jst', '不明')}",
        f"マップ: {analysis['map']} / Score: {analysis['scoreline']} / Queue: {analysis.get('queueId') or '-'}",
        f"KDA: {k}/{d}/{a} (K/D {analysis['kd']}) / ACS: {analysis['acs']}",
        f"HS率: {analysis['hs_rate']}% / 生存率: {analysis['survival_rate']}% / KAST(近似): {analysis['kast_like']}%",
    ]

    mk = analysis["multi_kills"]
    lines.append(
        "マルチキル: "
        f"2K {mk['2k']}回, 3K {mk['3k']}回, 4K {mk['4k']}回, 5K {mk['5k']}回"
    )
    lines.append(
        f"ACE: {analysis['ace_count']}回 / クラッチ: {analysis['clutch_count']}回 / "
        f"FB-FD: {analysis['first_blood_count']}-{analysis['first_death_count']}"
    )
    lines.append(f"3K以上: {analysis.get('triple_plus_kills', 0)}回")

    clutch_breakdown = analysis.get("clutch_breakdown", {}) or {}
    if clutch_breakdown:
        parts = [f"{k} {v}回" for k, v in sorted(clutch_breakdown.items())]
        lines.append("クラッチ内訳: " + ", ".join(parts))

    max_dmg = analysis["max_damage_round"]
    if max_dmg["roundNum"] is not None:
        lines.append(f"最大ダメージR: Round {max_dmg['roundNum']} で {max_dmg['damage']} dmg")

    narratives = analysis["narratives"]
    story = []
    if narratives["big_deficit_comeback_win"]:
        story.append("大差から追い上げて逆転")
    if narratives["lost_first_half_but_recovered"]:
        story.append("前半ビハインドから後半で取り返した")
    if narratives["overtime_battle"]:
        story.append("OT突入の死闘")
    if narratives["defense_collapsed"]:
        story.append("守りのラウンドを落とし過ぎた")
    if narratives["attack_worked"]:
        story.append("攻めの成功率が高かった")

    if story:
        lines.append("試合の流れ: " + " / ".join(story))
    if analysis.get("story_lines"):
        lines.append("ストーリー: " + " / ".join(analysis["story_lines"]))

    if not analysis["won"]:
        fail_points = []
        if analysis["first_death_count"] > analysis["first_blood_count"]:
            fail_points.append("先落ちが多め")
        if analysis["survival_rate"] < 25:
            fail_points.append("終盤まで残れるラウンドが少なめ")
        if narratives["defense_collapsed"]:
            fail_points.append("守備で流れを止められなかった")
        if fail_points:
            lines.append("敗因候補: " + " / ".join(fail_points))

    return "\n".join(lines)


# ★追加：LLM入力用（中身がある項目だけ出す）
def _thin_round_for_llm(r: dict) -> dict:
    # 必須（常に出す）
    out = {
        "roundNum": r.get("roundNum"),
        "side": r.get("side"),
        "won": r.get("won"),
        "scoreAfterRound": r.get("scoreAfterRound"),
        "kills": r.get("kills"),
        "damage": r.get("damage"),
    }

    # “中身がある場合だけ”出す（0/False/Noneならキーごと省略）
    if r.get("death"):
        out["death"] = True
    if r.get("assisted"):
        out["assisted"] = True
    if r.get("firstBlood"):
        out["firstBlood"] = True
    if r.get("firstDeath"):
        out["firstDeath"] = True

    mk = r.get("multiKill", 0) or 0
    if mk >= 2:
        out["multiKill"] = mk

    cav = r.get("clutchAttemptVs", 0) or 0
    if cav >= 2:
        out["clutchAttemptVs"] = cav
        if r.get("clutchWon"):
            out["clutchWon"] = True

    # もし「死んだ時間」も欲しくなったらここをONにできる（今は条件付きで出す）
    dt = r.get("deathTimeMs")
    if dt is not None:
        out["deathTimeMs"] = dt

    return out


def _build_llm_payload(riot_id: str, analysis: dict) -> dict:
    payload = {
        "riotId": riot_id,
        "match": {
            "map": analysis.get("map"),
            "queueId": analysis.get("queueId"),
            "won": analysis.get("won"),
            "scoreline": analysis.get("scoreline"),
        },
        "summary": {
            "kda": analysis.get("kda"),
            "kd": analysis.get("kd"),
            "acs": analysis.get("acs"),
            "hs_rate": analysis.get("hs_rate"),
            "first_blood_count": analysis.get("first_blood_count"),
            "first_death_count": analysis.get("first_death_count"),
            "multi_kills": analysis.get("multi_kills"),
            "max_damage_round": analysis.get("max_damage_round"),
            "story_lines": analysis.get("story_lines", []),
        },
        # ★各ラウンド：中身がある項目だけ出す
        "rounds": [
            _thin_round_for_llm(r)
            for r in (analysis.get("round_timeline") or [])
            if r is not None
        ],
    }

    # 空/冗長は出さない（質を落とさずトークン節約）
    if (analysis.get("ace_count") or 0) > 0:
        payload["summary"]["ace_count"] = analysis.get("ace_count")
        if analysis.get("ace_rounds"):
            payload["summary"]["ace_rounds"] = analysis.get("ace_rounds")

    if (analysis.get("clutch_count") or 0) > 0:
        payload["summary"]["clutch_count"] = analysis.get("clutch_count")
        if analysis.get("clutches"):
            payload["summary"]["clutches"] = analysis.get("clutches")
        if analysis.get("clutch_breakdown"):
            payload["summary"]["clutch_breakdown"] = analysis.get("clutch_breakdown")

    if analysis.get("narratives"):
        payload["narratives"] = analysis["narratives"]

    return payload


class InternalMe(APIView):
    """
    連携確認ではなく「直近1試合が取れる」ことを確認するためのエンドポイント
    - refresh 等はここではやらない（連携機能は無視）
    """

    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        discord_user_id = int(request.data.get("discord_user_id", 0))
        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).first()
        if not link:
            return Response({"ok": False, "error": "not_linked"}, status=404)

        if not link.riot_puuid:
            return Response({"error": "missing_puuid"}, status=404)

        api_key = getattr(settings, "RIOT_API_KEY", "")
        if not api_key:
            return Response({"error": "server_misconfigured_riot_api_key"}, status=500)

        region = getattr(settings, "VAL_MATCH_REGION", None) or (link.region or "ap")

        try:
            ml = matchlist_by_puuid(region, api_key, link.riot_puuid)
            history = ml.get("history", []) or []
            if not history:
                return Response({"error": "no_match_history"}, status=404)

            match_id = history[0].get("matchId")
            if not match_id:
                return Response({"error": "invalid_match_history"}, status=502)

            m = match_by_id(region, api_key, match_id)
            info = m.get("matchInfo", {}) or {}
            players = m.get("players", []) or []
            teams = m.get("teams", []) or []

            me = next((p for p in players if p.get("puuid") == link.riot_puuid), None)
            if not me:
                return Response({"error": "player_not_found_in_match"}, status=502)

            team_id = me.get("teamId")
            stats0 = (me.get("stats", {}) or {})
            kills = int(stats0.get("kills", 0))
            deaths = int(stats0.get("deaths", 0))
            assists = int(stats0.get("assists", 0))
            score = int(stats0.get("score", 0))
            rounds = int(stats0.get("roundsPlayed", 0)) or 1
            acs = round(score / rounds, 1)

            my_team = next((t for t in teams if t.get("teamId") == team_id), None)
            won = bool(my_team.get("won")) if my_team else None

            return Response(
                {
                    "ok": True,
                    "riotId": f"{link.riot_game_name}#{link.riot_tag_line}".strip("#"),
                    "puuid": link.riot_puuid,
                    "region": region,
                    "match": {
                        "matchId": match_id,
                        "map": _map_name(info.get("mapId", "")),
                        "mode": info.get("gameMode", "") or info.get("queueId", ""),
                        "isCompleted": bool(info.get("isCompleted", True)),
                        "won": won,
                        "k": kills,
                        "d": deaths,
                        "a": assists,
                        "acs": acs,
                        "teamId": team_id,
                        "gameStartMillis": info.get("gameStartMillis"),
                    },
                }
            )

        except Exception as e:
            return Response({"error": "match_fetch_exception", "detail": repr(e)}, status=502)


class InternalValorantRecentMatches(APIView):
    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        discord_user_id = int(request.data.get("discord_user_id", 0))
        count = int(request.data.get("count", 5))
        count = max(1, min(count, 10))

        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).first()
        if not link:
            return Response({"error": "not_linked"}, status=404)

        # PUUID が無ければ account-v1 で埋める（公式のみ）
        if not link.riot_puuid:
            if not (link.riot_game_name and link.riot_tag_line):
                return Response({"ok": False, "error": "not_linked_or_missing_riot_id"}, status=404)
            try:
                acct = account_by_riot_id(link.riot_game_name, link.riot_tag_line)
                puuid = acct.get("puuid", "") or ""
                if not puuid:
                    return Response({"ok": False, "error": "puuid_lookup_failed"}, status=502)
                link.riot_puuid = puuid
                link.save(update_fields=["riot_puuid", "updated_at"])
            except Exception as e:
                return Response({"ok": False, "error": "puuid_lookup_exception", "detail": repr(e)}, status=502)

        api_key = getattr(settings, "RIOT_API_KEY", "")
        if not api_key:
            return Response({"ok": False, "error": "server_misconfigured_riot_api_key"}, status=500)

        region = getattr(settings, "VAL_MATCH_REGION", None) or (link.region or "ap")

        ml = matchlist_by_puuid(region, api_key, link.riot_puuid)
        history = (ml.get("history", []) or [])[:count]

        items = []
        for h in history:
            match_id = h.get("matchId")
            if not match_id:
                continue

            m = match_by_id(region, api_key, match_id)
            info = m.get("matchInfo", {}) or {}
            players = m.get("players", []) or []
            teams = m.get("teams", []) or []

            me = next((p for p in players if p.get("puuid") == link.riot_puuid), None)
            if not me:
                continue

            team_id = me.get("teamId")
            stats0 = (me.get("stats", {}) or {})
            kills = int(stats0.get("kills", 0))
            deaths = int(stats0.get("deaths", 0))
            assists = int(stats0.get("assists", 0))
            score = int(stats0.get("score", 0))
            rounds = int(stats0.get("roundsPlayed", 0)) or 1
            acs = round(score / rounds, 1)

            my_team = next((t for t in teams if t.get("teamId") == team_id), None)
            won = bool(my_team.get("won")) if my_team else None

            items.append(
                {
                    "matchId": match_id,
                    "map": _map_name(info.get("mapId", "")),
                    "mode": info.get("gameMode", "") or info.get("queueId", ""),
                    "isCompleted": bool(info.get("isCompleted", True)),
                    "won": won,
                    "k": kills,
                    "d": deaths,
                    "a": assists,
                    "acs": acs,
                    "teamId": team_id,
                    "gameStartMillis": info.get("gameStartMillis"),
                }
            )

        return Response(
            {
                "ok": True,
                "riotId": f"{link.riot_game_name}#{link.riot_tag_line}".strip("#"),
                "puuid": link.riot_puuid,
                "region": region,
                "matches": items,
            }
        )


class InternalValorantMatchHighlight(APIView):
    permission_classes = [InternalAPIKeyPermission]

    def post(self, request):
        discord_user_id = int(request.data.get("discord_user_id", 0))
        if not discord_user_id:
            return Response({"error": "missing_discord_user_id"}, status=400)

        link = AccountLink.objects.filter(discord_user_id=discord_user_id).first()
        if not link:
            return Response({"error": "not_linked"}, status=404)

        if not link.riot_puuid:
            if not (link.riot_game_name and link.riot_tag_line):
                return Response({"error": "not_linked_or_missing_riot_id"}, status=404)
            try:
                acct = account_by_riot_id(link.riot_game_name, link.riot_tag_line)
                puuid = acct.get("puuid", "") or ""
                if not puuid:
                    return Response({"error": "puuid_lookup_failed"}, status=502)
                link.riot_puuid = puuid
                link.save(update_fields=["riot_puuid", "updated_at"])
            except Exception as e:
                return Response({"error": "puuid_lookup_exception", "detail": repr(e)}, status=502)

        api_key = getattr(settings, "RIOT_API_KEY", "")
        if not api_key:
            return Response({"error": "server_misconfigured_riot_api_key"}, status=500)

        region = getattr(settings, "VAL_MATCH_REGION", None) or (link.region or "ap")

        display_riot_id = f"{link.riot_game_name}#{link.riot_tag_line}".strip("#") or "未設定RiotID"

        try:
            ml = matchlist_by_puuid(region, api_key, link.riot_puuid)
            history = ml.get("history", []) or []
            if not history:
                return Response({"ok": False, "error": "no_match_history"}, status=404)

            latest_match_id = None
            latest_competitive_meta = None
            for h in history:
                if not h:
                    continue
                q = str(h.get("queueId", "") or "").strip().lower()
                if q == "competitive":
                    mid = h.get("matchId")
                    if mid:
                        latest_match_id = mid
                        latest_competitive_meta = h
                        break

            if not latest_match_id:
                return Response(
                    {
                        "ok": False,
                        "riotId": display_riot_id,
                        "region": region,
                        "error": "no_competitive_match_found",
                        "message": "match history内にCompetitiveの試合が存在しませんでした。",
                    },
                    status=404,
                )

            match_data = match_by_id(region, api_key, latest_match_id)
            analysis = _compute_match_highlights(match_data, link.riot_puuid)
            game_start_ms = _safe_int((latest_competitive_meta or {}).get("gameStartTimeMillis"), 0)
            if game_start_ms <= 0:
                game_start_ms = _safe_int(((match_data.get("matchInfo", {}) or {}).get("gameStartMillis")), 0)
            analysis["game_start_jst"] = _format_jst_datetime_from_millis(game_start_ms)
            riot_id = display_riot_id
            message = _build_discord_match_message(riot_id, analysis)

            # ★追加：LLMへ投げる専用ペイロード（中身があるキーだけ出る）
            llm_payload = _build_llm_payload(riot_id, analysis)

            return Response(
                {
                    "ok": True,
                    "riotId": riot_id,
                    "region": region,
                    "gameStartAtJST": analysis["game_start_jst"],
                    "llm_payload": llm_payload,  # ★ここをLLM入力に使う
                    "discord_message": message,
                }
            )
        except ValueError as e:
            return Response({"ok": False, "error": str(e)}, status=502)
        except Exception as e:
            return Response({"ok": False, "error": "highlight_fetch_exception", "detail": repr(e)}, status=502)
