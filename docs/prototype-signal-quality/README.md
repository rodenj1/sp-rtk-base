# PROTOTYPE, throwaway: Signal Quality UI

Branch-only. Answers rodenj1/rtk_development#7: where and how do the Signal
Quality verdict and its numbers appear on survey-in and the dashboard?

Run (fake receiver, no hardware):

    SP_RTK_BASE_FAKE_GPS=1 SP_RTK_BASE_PROTOTYPE=1 SP_RTK_BASE_PORT=8091 uv run python -m sp_rtk_base.main
    curl -X POST localhost:8091/api/device/connect -H 'Content-Type: application/json' \
         -d '{"vendor":"fake","port":"FAKE","baud_rate":115200}'

Open `/survey` or `/` with `?variant=A|B|C&scenario=good|marginal|poor|l1only|nodata|nomsm`.

Verdict: B (Signal card) on both pages, with a per-page setting to switch
either page to A (Title chip). The screenshots here are from this run.
