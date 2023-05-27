Nest Web Client
===============

Nest thermostat control via undocumented web API.  Subject to break if the API changes.


Configuration
-------------

Example configuration file::

    [credentials]
    email = ...

    [device]
    serial = ...

    [oauth]
    cookie = ...
    login_hint = ...
    client_id = ...

    [units]
    temperature = f


The ``oauth`` section values currently need to be obtained manually by using devtools in Chrome...

.. note::

    While it may be possible to follow this procedure with Firefox, the value of the ``cookie`` obtained in the last
    step seems to be slightly different.  Attempting to use it without editing it may cause an encoding error.  Rather
    than trying to find the correct content to include/exclude, it may be easier to just use Chrome for this instead.

The steps to do so:

- Log out of your Nest account in Chrome

  - This step may not be necessary - simply accessing the page in a new tab may be sufficient
- Open a new tab, open devtools (ctrl+shift+i), and go to the Network tab

  - It may help to check ``Disable Cache``
- Go to ``home.nest.com``, click ``Sign in with Google``, and log in

  - If you have 3rd party cookies disabled / blocked in your browser's settings, you will need to temporarily allow
    them to do so.
- In devtools, filter to ``issueToken``, click the ``iframerpc`` row, and examine the ``Request URL``.  The
  ``login_hint`` and ``client_id`` values can be extracted from the query parameters

  - Note: This is slightly different from the config used by badnest
  - While the ``cookie`` value from the next step may need to be updated periodically (usually after a few months),
    these 2 values appear to stay consistent.
- Filter to ``oauth2/iframe`` and click the last ``iframe`` row.  The ``cookie`` value is the entire ``cookie`` value
  from the ``Request Headers`` for this row.

Thanks to the `badnest <https://github.com/therealryanbonham/badnest>`_ project for the OAuth login info
