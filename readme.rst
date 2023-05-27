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


The ``oauth`` section values currently need to be obtained manually by using devtools in Chrome...  Steps:

- Log out of your Nest account in Chrome
- Open a new tab, open devtools (ctrl+shift+i), and go to the Network tab
- Go to ``home.nest.com``, click ``Sign in with Google``, and log in
- In devtools, filter to ``issueToken``, click the ``iframerpc`` row, and examine the ``Request URL``.  The
  ``login_hint`` and ``client_id`` values can be extracted from the query parameters

  - Note: This is slightly different from the config used by badnest
- Filter to ``oauth2/iframe`` and click the last ``iframe`` row.  The ``cookie`` value is the entire ``cookie`` value
  from the ``Request Headers`` for this row.

Thanks to the `badnest <https://github.com/therealryanbonham/badnest>`_ project for the OAuth login info
