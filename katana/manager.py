#!/usr/bin/env python3
""" A katana manager which is capable managing the evaluation of arbitrary
units against an arbitrary number of Targets of varying types in a
multithreaded manner and reporting results to a Monitor object """
from dataclasses import dataclass, field
from typing import List, Any
import configparser
import threading
import logging
import queue
import time
import os
import re

# Katana imports
from katana.target import Target
from katana.unit import Unit, Finder
from katana.monitor import Monitor
import katana.util

class Manager(configparser.ConfigParser):
	""" Class to manage the threaded evaluation of applicable units against
	arbitrary targets. Facilitates work queue management and recursion within
	given units. It will also manage output file creation (such as artifacts).
	"""

	@dataclass(order=True)
	class WorkItem(object):
		""" Defines the items that are actually placed in the work queue. The
		work queue maintains the state of the case generator and priority for
		the unit. Priority is taken directly from the unit. `generator` is the
		result of `unit.evaluate` and will be called when the first thread
		begins evaluating the unit. """
		priority: int
		action: str=field(compare=False)
		unit: Unit=field(compare=False)
		generator: Unit=field(compare=False)

	def __init__(self, monitor: Monitor = None, config_path=None, default_units=True):
		super(Manager, self).__init__()

		# Default values for configuration items
		self['DEFAULT'] = {
			'unit': [],
			'threads': len(os.sched_getaffinity(0)),
			'outdir': './results',
			'auto': False,
			'recurse': True,
			'exclude': [],
			'min-data': 10,
			'download': False,
			'template': 'default',
			'timeout': 0.1,
			'password': [],
			'prioritize': True,
			'default-units': True,
			'max-depth': 10
		}

		self['manager'] = {}

		# Load a configuration file if specified
		if config_path is not None and len(self.read(config_path)) == 0:
			raise RuntimeError('{0}: configuration file not found')

		# Create a default monitor if there is none
		if monitor is None:
			monitor = Monitor()
		
		# Create the unit finder for matching targets to units
		self.finder = Finder(self, use_default = default_units)
		# This is the work queue
		self.work = queue.PriorityQueue()
		# This is the barrier which signals wait on and signals completion of
		# evaluation. It is initialized in the `start` method
		self.barrier = None
		# Array of threads (also initialized in `start`)
		self.threads = []
		# Flag pattern will be compiled upon running `start`
		self.flag_pattern = None
		# Save the monitor
		self.monitor = monitor

	def register_artifact(self, unit: Unit, path: str,
			recurse: bool = True) -> None:
		""" Register an artifact result with the manager """
	
		# Notify the monitor of an artifact
		self.monitor.on_artifact(self, unit, path)

		# Recurse on this target
		if self['manager'].getboolean('recurse') and recurse:
			self.queue_target(path, parent=unit)
	
	def register_data(self, unit: Unit, data: Any,
			recurse: bool = True) -> None:
		""" Register arbitrary data results with the manager """
		
		# Notify the monitor of the data
		self.monitor.on_data(self, unit, data)

		# Look for flags
		self.find_flag(unit, data)

		if self['manager'].getboolean('recurse') and not unit.origin.completed \
				and recurse:
			# Only do full recursion if requested
			self.queue_target(data, parent=unit)
	
	def register_flag(self, unit: Unit, flag: str) -> None:
		""" Register a flag that was found during processing and raise the
		FoundFlag exception. """

		# Notify the monitor
		self.monitor.on_flag(self, unit, flag)

		# Mark this unit as completed
		unit.origin.completed = True

	def find_flag(self, unit: Unit, data: Any) -> None:
		""" Search arbitrary data for flags matching the given flag format in
		the manager configuration """
		
		# Iterate over lists and tuples automatically
		if isinstance(data, list) or isinstance(data, tuple):
			for item in data:
				self.find_flag(unit, item)
			return

		# We deal with bytes here
		if isinstance(data, str):
			data = data.encode('utf-8')

		# CALEB: this is a hack to remove XML from flags, and check that as
		# well. It was observed to be needed for some weird XML challenges.
		no_xml = re.sub(b'<[^<]+>', b'', data)
		if no_xml != data:
			self.find_flag(unit, no_xml)

		# Search the data for flags
		match = self.flag_pattern.search(data)
		if match:
			# Flags should be printable
			found = match.group().decode('utf-8')
			if katana.util.isprintable(found):
				# Strict flags means that the flag will be alone in the output
				if unit.STRICT_FLAGS and len(found) == len(data):
					self.register_flag(unit, found)
				elif not unit.STRICT_FLAGS:
					self.register_flag(unit, found)


	def target(self, upstream: bytes, parent: Unit = None) -> Target:
		""" Build a new target in the context of this manager """
		return Target(self, upstream, parent)

	def validate(self) -> None:
		""" Validate the configuration given this manager, a target, and a set
		of chosen units you are going to run. Not verify the validity could
		cause unexpected errors later when running your units """

		# Ensure the required global values are present in the configuration
		if 'flag-format' not in self['manager']:
			raise RuntimeError("manager: flag-format not specified")

		self.finder.validate()

	def queue_target(self, upstream: bytes, parent: Unit = None) -> Target:
		""" Create a target, enumerate units, queue them, and return the target
		object """

		# That's silly...
		if upstream.strip() == '':
			return None

		# Don't recurse if the parent is already done
		if parent is not None:
			# Don't queue recursion for a completed target
			if parent.origin.completed:
				return None
			# Maximum depth reached!
			if (parent.depth+1) >= self['manager'].getint('max-depth'):
				self.monitor.on_depth_limit(self, parent.target, parent)
				return None

		# Create the target object
		target = self.target(upstream, parent)
	
		# Enumerate valid units
		for unit in self.finder.match(target):
			self.queue(unit)

		# Return the target object
		return target

	
	def queue(self, unit: Unit) -> None:
		""" Queue the given unit to be evaluated. This will add the unit to the
		queue given it's prioritization, and the unit will be evaluated once
		the manager is started. If the manager has already been started, the
		unit will be evaluated based on it's priority the next time a thread is
		free. """

		# Check if we are completed
		if unit.origin.completed:
			return

		item = Manager.WorkItem(unit.PRIORITY,	# Unit priority
								'init',			# Initialization of work item
								unit,			# The unit itself
								None)			# The generator to get the next case

		# Queue the item for usage
		self.work.put(item)

		# Ensure sleeping threads wake up
		if self.barrier is not None:
			self.barrier.reset()
	
	def requeue(self, item: WorkItem) -> None:
		""" Requeue an item which has more cases left to evaluate """

		# Don't requeue completed items
		if item.unit.origin.completed:
			return

		# We aren't initializing anymore
		item.action = 'evaluate'

		# Requeue the item
		self.work.put(item)

	def start(self) -> None:
		""" Start the needed threads and begin evaluation of units. You can
		still add units to the queue for evaluation after start is called up
		until you call `Manager.join`.

		Targets can continue to be added up to the point that you call join.
		After that, any new target addition will generate an exception, unless
		there is a parent unit specified (aka, the target is due to recursion).
		"""

		# Prepare the results directory
		self._prepare_results()

		# Validate the configuration items are valid and there will be no
		# issues moving forward
		self.validate()

		# Compile the flag pattern
		self.flag_pattern = re.compile(
				bytes(self['manager']['flag-format'],'utf-8'),
				re.DOTALL | re.MULTILINE | re.IGNORECASE)

		# Create the barrier object
		self.barrier = threading.Barrier(self['manager'].getint('threads')+1)
		self.threads = [None]*self['manager'].getint('threads')

		# Start the threads (will automatically begin processing units)
		for n in range(len(self.threads)):
			self.threads[n] = threading.Thread(target=self._thread)
			self.threads[n].start()

	def join(self, timeout = None) -> bool:
		""" Wait for all work to complete. Depending on your Finder and Target,
		this may take some time, and may time out before completion.

		After joining, no more root targets (those without parents) can be
		queued (this will result in an exception). One all targets have
		finished processing (including recursion/child targets), join will
		return.

		"""

		# Record starting and expected ending time to comply with timeout
		if timeout is not None:
			join_time = time.time()
			stop_time = join_time + timeout
		# Indicates we have already requested threads to cleanly exit
		aborting = True
		# Timeout was hit
		did_timeout = False

		while True:
			
			try:
				# Wait for all threads to meet the barrier, which indicates all
				# unit/case pairs are processed
				if timeout is not None:
					self.barrier.wait(stop_time - time.time())
				else:
					self.barrier.wait()
			except threading.BrokenBarrierError:
				pass
			except KeyboardInterrupt:

				# If we have already signaled, and we catch another
				# Ctrl+C, just exit and let the user force quit at the
				# threading `join` call below
				if aborting == True:
					break
				# Signal threads to exit cleanly after current unit/case pair
				# evaluation is completed.
				self._signal_complete()
				aborting = True
			else:
				# Just in case, we signal completion, but all threads should
				# exit when the barrier is reached successfully
				self._signal_complete()
				break

			# Signal completion if our timeout has expired
			if timeout is not None and time.time() >= stop_time:
				did_timeout = True
				self._signal_complete()
				break

		# Wait on all threads to complete
		for thread in self.threads:
			thread.join()

		# Notify the monitor that we are done
		self.monitor.on_completion(self, did_timeout)

		return not did_timeout

	def _signal_complete(self) -> None:
		""" Send work items with high priority to signal closing down threads
		"""

		for thread in self.threads:
			self.work.put(Manager.WorkItem(	-10000,		# Priority
											'abort',	# Action
											None,		# Unit
											None))		# Generator

		# Make all the workers wake up and grab these events
		self.barrier.reset()

	def _thread(self) -> None:
		""" This is the main method for each evaluator thread. It will monitor
		the work queue, and evaluate units as they become available. The
		threads are started by the ``Manager.start`` method. """

		while True:

			try:
				# Attempt to grab work from the queue
				work = self.work.get(False)
			except queue.Empty:
				try:
					# Signal this thread is waiting for work
					# check again every 0.2 seconds, just in case
					self.barrier.wait()
				except threading.BrokenBarrierError:
					# A new unit was queued or the timeout happened
					# Check for new units
					continue
				else:
					# All threads hit the barrier, we are free to exit
					break

			# The parent is asking nicely to exit
			if work.action == 'abort':
				break

			# Ignore the unit if it is already completed
			if work.unit.origin.completed:
				continue

			if work.action == 'init':
				# This is the first time the unit has run, so we need to
				# initialize the generator for cases
				work.generator = work.unit.enumerate()

			# We have a unit to process, grab the next case
			try:
				case = next(work.generator)
			except StopIteration:
				# We are done with this item, continue processing
				continue

			# Before we evaluate, place this case back on the queue in order to
			# allow parallel processing of the cases
			self.requeue(work)
			
			try:
				# Evaluate this case
				work.unit.evaluate(case)
			except Exception as e:
				# We got an exception, notify the monitor and continue
				self.monitor.on_exception(self, work.unit, e)
	
	def _prepare_results(self) -> None:
		""" Prepare the results directory to house all artifacts and results
		from this run of katana. This is automatically called when `start` is
		executed, and will create the output directory.

		This function will raise an exception if the chosen output directory
		already exists. """

		# Create the directory tree for the output
		os.makedirs(self['manager']['outdir'])