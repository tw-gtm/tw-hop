import os
import time
import random
import logging
import requests
from trustwise.utils import validate_api_key
from trustwise.dtos.models import LoggingPayload
from dotenv import load_dotenv
from datetime import datetime
from collections import defaultdict
from typing import Any, Dict, List, Optional
from llama_index.callbacks.base_handler import BaseCallbackHandler
from llama_index.callbacks.schema import (
    BASE_TRACE_EVENT,
    TIMESTAMP_FORMAT,
    CBEvent,
    CBEventType,
    EventStats,
)

# Load Environment
load_dotenv()


class TrustwiseCallbackHandler(BaseCallbackHandler):
    """Trustwise Callback handler that keeps log of events and event info.

        NOTE: this is a beta feature. The usage within our codebase, and the interface
        may change.

        This handler keeps track and logs of event starts/ends, separated by event types.
        Generates an Experiment ID to track evaluations and pipeline events under one umbrella.
        Uploads the logs to MongoDB - part of the Safety System of Record.

        Args:
            event_starts_to_ignore (Optional[List[CBEventType]]): list of event types to
                ignore when tracking event starts.
            event_ends_to_ignore (Optional[List[CBEventType]]): list of event types to
                ignore when tracking event ends.

        """

    def __init__(
            self,
            event_starts_to_ignore: Optional[List[CBEventType]] = None,
            event_ends_to_ignore: Optional[List[CBEventType]] = None,
            print_trace_on_end: bool = True,
    ) -> None:
        """Initialize the Trustwise Callback handler."""

        self._user_id = None
        self.experiment_id = None
        self._event_pairs_by_type: Dict[CBEventType, List[CBEvent]] = defaultdict(list)
        self._event_pairs_by_id: Dict[str, List[CBEvent]] = defaultdict(list)
        self._sequential_events: List[CBEvent] = []
        self._cur_trace_id: Optional[str] = None
        self._trace_map: Dict[str, List[str]] = defaultdict(list)
        self.print_trace_on_end = print_trace_on_end
        event_starts_to_ignore = (
            event_starts_to_ignore if event_starts_to_ignore else []
        )
        event_ends_to_ignore = event_ends_to_ignore if event_ends_to_ignore else []
        super().__init__(
            event_starts_to_ignore=event_starts_to_ignore,
            event_ends_to_ignore=event_ends_to_ignore,
        )

    def set_experiment_id(self):
        """
        Function to generate unique experiment id
        :return: unique_id
        """
        # Get the current timestamp
        timestamp = int(time.time() * 1000)  # Multiply by 1000 to get milliseconds

        # Generate a random component
        random_part = random.randint(100000, 999999)  # 6-digit random number

        # Combine timestamp and random component
        unique_id = f"{timestamp}{random_part}"

        unique_id = unique_id[:8]  # Ensure it is exactly 8 digits
        self.experiment_id = unique_id  # Set experiment id

        # User reminder to note experiment id
        print("Please keep a note of your Experiment ID:", self.experiment_id)
        logging.info(f"Experiment ID : {self.experiment_id}")

        return unique_id

    def set_api_key(self, api_key: str):
        """
        Function to set user API Key in the callback and returns User ID
        :param api_key: Trustwise API Key
        :return: None
        """
        if api_key is not None:
            self._user_id = validate_api_key(api_key)
            print(f"API Key is Authenticated! - User ID : {self._user_id}")
        else:
            logging.error(f"API Key is invalid!, Please visit -> {os.getenv('github_login_url')}")
            raise ValueError("API Key is invalid!")

    def on_event_start(
            self,
            event_type: CBEventType,
            payload: Optional[Dict[str, Any]] = None,
            event_id: str = "",
            parent_id: str = "",
            **kwargs: Any,
    ) -> str:
        """Store event start data by event type.

        Args:
            event_type (CBEventType): event type to store.
            payload (Optional[Dict[str, Any]]): payload to store.
            event_id (str): event id to store.
            parent_id (str): parent event id.

        """
        event = CBEvent(event_type, payload=payload, id_=event_id)
        self._event_pairs_by_type[event.event_type].append(event)
        self._event_pairs_by_id[event.id_].append(event)
        self._sequential_events.append(event)

        # Log event to MongoDB
        self.log_to_mongodb(event, parent_id)

        return event.id_

    def on_event_end(
            self,
            event_type: CBEventType,
            payload: Optional[Dict[str, Any]] = None,
            event_id: str = "",
            **kwargs: Any,
    ) -> None:
        """Store event end data by event type.

        Args:
            event_type (CBEventType): event type to store.
            payload (Optional[Dict[str, Any]]): payload to store.
            event_id (str): event id to store.

        """
        event = CBEvent(event_type, payload=payload, id_=event_id)
        self._event_pairs_by_type[event.event_type].append(event)
        self._event_pairs_by_id[event.id_].append(event)
        self._sequential_events.append(event)

        self.log_to_mongodb(event)  # Log events to MongoDB

        self._trace_map = defaultdict(list)

    # Function to log events to MongoDB
    def log_to_mongodb(self, event: CBEvent, parent_id: str = "") -> None:
        """

        :param event: Callback Event from Llama Index to be stored in the logs
        :param parent_id: Parent event ID, if the event is a parent then 'root'
        :return:
        """
        try:
            payload = LoggingPayload(
                user_id=self._user_id,
                experiment_id=self.experiment_id,
                trace_type=self._cur_trace_id,
                event_type=event.event_type.name,
                parent_id=parent_id if parent_id else event.id_,
                event_id=event.id_,
                event_time=event.time,
                event_payload=event.payload.to_dict() if hasattr(event.payload, 'to_dict') else str(event.payload),
            )

            payload_dict = payload.model_dump()  # Convert to dict for JSON serialization
            response = requests.post(url=os.getenv('log_events_url'), json=payload_dict)
            response.raise_for_status()  # Raise HTTPError for bad responses

            logging.info("Event logged to MongoDB Successfully")

        except requests.exceptions.RequestException as e:  # Handle request exceptions
            logging.error(f"Error logging event to MongoDB: {e}")

        except Exception as e:
            logging.error(f"Unexpected error: {e}")  # Handle Unexpected Errors

    def get_events(self, event_type: Optional[CBEventType] = None) -> List[CBEvent]:
        """Get all events for a specific event type."""
        if event_type is not None:
            return self._event_pairs_by_type[event_type]

        return self._sequential_events

    def _get_event_pairs(self, events: List[CBEvent]) -> List[List[CBEvent]]:
        """Helper function to pair events according to their ID."""
        event_pairs: Dict[str, List[CBEvent]] = defaultdict(list)
        for event in events:
            event_pairs[event.id_].append(event)

        return sorted(
            event_pairs.values(),
            key=lambda x: datetime.strptime(x[0].time, TIMESTAMP_FORMAT),
        )

    def _get_time_stats_from_event_pairs(
            self, event_pairs: List[List[CBEvent]]
    ) -> EventStats:
        """Calculate time-based stats for a set of event pairs."""
        total_secs = 0.0
        for event_pair in event_pairs:
            start_time = datetime.strptime(event_pair[0].time, TIMESTAMP_FORMAT)
            end_time = datetime.strptime(event_pair[-1].time, TIMESTAMP_FORMAT)
            total_secs += (end_time - start_time).total_seconds()

        return EventStats(
            total_secs=total_secs,
            average_secs=total_secs / len(event_pairs),
            total_count=len(event_pairs),
        )

    def get_event_pairs(
            self, event_type: Optional[CBEventType] = None
    ) -> List[List[CBEvent]]:
        """Pair events by ID, either all events or a specific type."""
        if event_type is not None:
            return self._get_event_pairs(self._event_pairs_by_type[event_type])

        return self._get_event_pairs(self._sequential_events)

    def get_llm_inputs_outputs(self) -> List[List[CBEvent]]:
        """Get the exact LLM inputs and outputs."""
        return self._get_event_pairs(self._event_pairs_by_type[CBEventType.LLM])

    def get_event_time_info(
            self, event_type: Optional[CBEventType] = None
    ) -> EventStats:
        event_pairs = self.get_event_pairs(event_type)
        return self._get_time_stats_from_event_pairs(event_pairs)

    def flush_event_logs(self) -> None:
        """Clear all events from memory."""
        self._event_pairs_by_type = defaultdict(list)
        self._event_pairs_by_id = defaultdict(list)
        self._sequential_events = []

    def start_trace(self, trace_id: Optional[str] = None) -> None:
        """Launch a trace."""
        self._trace_map = defaultdict(list)
        self._cur_trace_id = trace_id

    def end_trace(
            self,
            trace_id: Optional[str] = None,
            trace_map: Optional[Dict[str, List[str]]] = None,
    ) -> None:
        """Shutdown the current trace."""
        self._trace_map = trace_map or defaultdict(list)
        if self.print_trace_on_end:
            self.print_trace_map()

    def _print_trace_map(self, cur_event_id: str, level: int = 0) -> None:
        """Recursively print trace map to terminal for debugging."""
        event_pair = self._event_pairs_by_id[cur_event_id]
        if event_pair:
            time_stats = self._get_time_stats_from_event_pairs([event_pair])
            indent = " " * level * 2
            print(
                f"{indent}|_{event_pair[0].event_type} -> ",
                f"{time_stats.total_secs} seconds",
                flush=True,
            )

        child_event_ids = self._trace_map[cur_event_id]
        for child_event_id in child_event_ids:
            self._print_trace_map(child_event_id, level=level + 1)

    def print_trace_map(self) -> None:
        """Print simple trace map to terminal for debugging of the most recent trace."""
        print("*" * 15, flush=True)
        print(f"Trace: {self._cur_trace_id}", flush=True)
        self._print_trace_map(BASE_TRACE_EVENT, level=1)
        print("*" * 15, flush=True)

    @property
    def event_pairs_by_type(self) -> Dict[CBEventType, List[CBEvent]]:
        return self._event_pairs_by_type

    @property
    def events_pairs_by_id(self) -> Dict[str, List[CBEvent]]:
        return self._event_pairs_by_id

    @property
    def sequential_events(self) -> List[CBEvent]:
        return self._sequential_events
