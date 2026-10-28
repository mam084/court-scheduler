"""The Streamlit app, split into the steps each page run goes through and one module per section.

main.run() is the whole page, top to bottom:

    setup.run()        password / Google sign-in, the database, the Google connection
    session            the club's one schedule and its runs of dates (st.session_state)
    sidebar.render()   Dates menu, new dates, import, solver settings, Auto-schedule
    build.run()        these dates' inputs and settings; the solver when asked
    header.render()    title, summary numbers, coverage flags, warnings, section menu
    tabs.<x>.render()  the chosen section (only that one runs)
    session.autosave() save if anything changed

Values that several steps share for one page run (the inputs, the dates,
the signed-in person, whether they're an admin...) live on `ui.state.S`.
"""
