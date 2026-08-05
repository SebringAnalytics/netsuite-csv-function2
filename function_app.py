import logging
import azure.functions as func
from netsuite_loader import run_load

app = func.FunctionApp()

@app.timer_trigger(
    schedule="%NETSUITE_LOAD_SCHEDULE%",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def netsuite_csv_timer(timer: func.TimerRequest) -> None:
    logging.info("NetSuite CSV load started.")
    if timer.past_due:
        logging.warning("Timer invocation is past due.")
    try:
        run_load()
    except Exception:
        logging.exception("NetSuite CSV load failed.")
        raise
    logging.info("NetSuite CSV load completed successfully.")
