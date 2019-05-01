from unit import BaseUnit
from collections import Counter
import sys
from io import StringIO
import argparse
from pwn import *
import os
import units.crypto
from units import NotApplicable
import string
import collections

def decryptFence(cipher, rails, offset=0):
	plain = ''

	# offset
	if offset:
		t = encryptFence('o'*offset + 'x'*len(cipher), rails)
		for i in range(len(t)):
			if(t[i] == 'o'):
				cipher = cipher[:i] + '#' + cipher[i:]
	
	length = len(cipher)
	fence = [['#']*length for _ in range(rails)]

	# build fence
	i = 0
	for rail in range(rails):
		p = (rail != (rails-1))
		x = rail
		while (x < length and i < length):
			fence[rail][x] = cipher[i]
			if p:
				x += 2*(rails - rail - 1)
			else:
				x += 2*rail
			if (rail != 0) and (rail != (rails-1)):
				p = not p
			i += 1

	# read fence
	for i in range(length):
		for rail in range(rails):
			if fence[rail][i] != '#':
				plain += fence[rail][i]
	return plain


# class Unit(units.PrintableDataUnit):
class Unit(units.NotEnglishUnit):

	PROTECTED_RECURSE = True

	@classmethod
	def add_arguments(cls, katana, parser):
		parser.add_argument('--railfence-rails', type=int,
			help='number of rails to use for railfence cipher',
			default=0, required = False)
		parser.add_argument('--railfence-offset', type=int,
			help='initial offset for railfence cipher',
			default=0, required = False)

	def evaluate(self, katana, case):
		
		seen_plaintext = []
		if katana.config['railfence_rails'] not in range(2,100):
			number_of_rails = [ 2 ]
		if not katana.config['railfence_rails']:
			number_of_rails = range(2,100)

		for i in number_of_rails:

			plaintext = decryptFence(self.target.stream.read().decode('utf-8'), i, offset=0)
			if plaintext not in seen_plaintext:

				seen_plaintext.append(plaintext)

				katana.recurse(self, plaintext)

				# STRICT: Only report a flag match if it matches, END TO END!
				if katana.locate_flags(self, plaintext, strict = True):
					self.completed = True
				katana.add_results(self, plaintext)