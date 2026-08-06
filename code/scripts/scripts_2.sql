with main as (
    select
        m.id,
        m.dep_id
    from
        cbs.main as m
    union all
    select
        c.id,
        c.dep_id
    from
        cbs.other as c
)
select
    user.id
from
    CBS.C_USER as user
inner join
    (
        select
            Distinct on (id, dep_id)
            m3.id,
            m3.dep_id
        from
            main as m3
        order by
            id,
            dep_id
    ) as m
    on m.id = user.main_id
    and m.dep_id = user.dep_id